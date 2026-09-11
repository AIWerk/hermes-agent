"""Behavior guards for the bounded unread/contact restoration."""
import json
import pytest

@pytest.fixture
def ws(monkeypatch, tmp_path):
    for key in list(__import__('os').environ):
        if key.startswith(('AIWERK_CUI_', 'HIMALAYA_')) or key == 'MAILDIR':
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    from hermes_cli import web_server
    return web_server

def mail(id, sender='Ada <ada@example.org>', **kw):
    return {'id': id, 'sender': sender, **kw}

@pytest.mark.parametrize('backend', ['gmail', 'himalaya'])
@pytest.mark.parametrize('blocked_count', [1, 8])
def test_backend_fetches_unread_before_latest_topup(ws, monkeypatch, backend, blocked_count):
    monkeypatch.setattr(ws, '_ASSISTANT_EMAIL_PREVIEW_ITEMS', 3)
    unread = [mail('spam'+str(i), 'Offer <x@attractivewedding.info>') for i in range(blocked_count)] + [mail('old-unread')]
    latest = [mail('read-new'), mail('old-unread'), mail('read-next')]
    calls = []
    if backend == 'gmail':
        def bridge(config, *, server, tool, params):
            calls.append((tool, params))
            if tool == 'search_gmail_messages':
                return {'messages': unread if 'unread' in params['query'] else latest}
            ids = params['message_ids']
            return {'messages': [item for item in (unread if ids[0].startswith('spam') else latest) if item['id'] in ids]}
        monkeypatch.setattr(ws, '_call_aiwerk_bridge_tool', bridge)
        result = ws._google_workspace_email_summary({}, {'backend': 'gmail', 'address': 'me@example.org'})
        assert any(tool == 'get_gmail_messages_content_batch' and 'old-unread' in p['message_ids'] and p['message_ids'][0].startswith('spam') for tool, p in calls)
    else:
        def envelopes(query=None, **kw):
            calls.append(query)
            return unread if query else latest
        monkeypatch.setattr(ws, '_run_himalaya_envelope_list', envelopes)
        result = ws._himalaya_email_summary({}, {'backend': 'himalaya', 'account': 'main'})
        assert calls[0] and 'Seen' in str(calls[0])
    assert [i['id'] for i in result['items']] == ['old-unread', 'read-new', 'read-next']
    assert [i['unread'] for i in result['items']] == [True, False, False]
    assert result['unread_count'] == 1
    assert result['filtered_count'] == blocked_count
    merged = ws._merge_email_summaries([{'items': [mail('read-first', unread=False)], 'unread_count': 0}, result])
    assert merged['items'][0]['id'] == 'old-unread'
    assert merged['filtered_count'] == blocked_count

def test_unread_helper_dedupes_without_mutating_and_keeps_all_unread(ws):
    shaped = ws._unread_first_email_items([{'id': 'spam', 'from': {'addr': 'x@attractivewedding.info'}}, {'id': 'ok', 'from': {'name': 'Ada', 'addr': 'ada@example.org'}, 'date': '2026-09-11T09:00:00Z'}])
    assert [x['id'] for x in shaped] == ['ok']
    assert shaped[0]['sender'] == 'Ada <ada@example.org>'
    assert shaped[0]['received_at'] == '2026-09-11T09:00:00Z'
    original = [mail('a'), mail('a'), mail('b')]
    result = ws._unread_first_email_items(original, [mail('b'), mail('c'), mail('c'), mail('d')], min_items=4)
    assert [i['id'] for i in result] == ['a', 'b', 'c', 'd']
    assert 'unread' not in original[0]
    assert len(ws._unread_first_email_items([mail('a'), mail('b')], min_items=1)) == 2

@pytest.mark.parametrize('value, expected', [('Name <ADA@Example.ORG>\x00', 'ada@example.org'), ('not an email', ''), ('x'*254+' a@example.org', '')])
def test_contact_email_sanitization(ws, value, expected):
    assert ws._safe_contact_email(value) == expected
    assert ws._normalize_contact_item({'email': value})['email'] == expected

@pytest.mark.parametrize('value, expected', [(' +41\x0079\n123 ', '+41 79 123'), ('unavailable', ''), ('1'*100, '1'*80)])
def test_contact_phone_sanitization(ws, value, expected):
    assert ws._safe_contact_phone(value) == expected
    assert ws._normalize_contact_item({'phone': value})['phone'] == expected

def test_badges_normalize_merge_and_remove_own_address(ws):
    values = [' Gmail\x00', 'gmail', 'CRM', 'Other', 'Fourth', 'Fifth']
    assert ws._dedupe_contact_badges(values) == ['Gmail', 'CRM', 'Other', 'Fourth']
    normalized = ws._normalize_contact_item({'email': 'ada@example.org', 'source_badges': values})
    assert normalized['source_badges'] == ['Gmail', 'CRM', 'Other', 'Fourth']
    merged = ws._dedupe_contacts([{'email': 'ADA@example.org', 'source_badges': ['Gmail']}, {'email': 'ada@example.org', 'source_badges': ['gmail', 'CRM']}])
    assert merged[0]['source_badges'] == ['Gmail', 'CRM']
    payload = ws._filter_contacts_payload({'items': [{'email': 'ada@example.org', 'source_badges': ['Me <ME@example.org>', 'CRM']}]}, own_emails={'me@example.org'})
    assert payload['items'][0]['source_badges'] == ['CRM']

def test_contact_consumers_extract_addresses_and_exclude_own(ws, monkeypatch):
    monkeypatch.setattr(ws, '_email_account_configs', lambda c: [{'address': 'Me <ME@example.org>'}])
    monkeypatch.setattr(ws, '_calendar_accounts', lambda c: [])
    monkeypatch.setattr(ws, '_contact_account_configs', lambda c: [])
    own = ws._contacts_own_email_set({}, {'accounts': [{'account_address': 'Other <OTHER@example.org>'}]}, {})
    assert own == {'me@example.org', 'other@example.org'}
    assert not ws._contact_is_customer_safe({'email': 'Me <ME@example.org>'}, own)
    assert 'email:ada@example.org' in ws._contact_hide_keys({'email': 'Ada <ADA@example.org>'})
    parsed = ws._contacts_from_address_text('Ada <ADA@example.org>, bad <invalid>', source='Gmail')
    assert [x['email'] for x in parsed] == ['ada@example.org']
    parsed = ws._parse_google_contacts('Contact ID: one\nName: Ada\nEmail: Ada <ADA@example.org> (work)\nPhone: +41 79 123 (mobile)')
    assert parsed[0]['email'] == 'ada@example.org'
    assert parsed[0]['phone'] == '+41 79 123'

def test_persisted_contacts_are_normalized_on_read_and_write(ws, tmp_path):
    raw = {'email': 'Ada <ADA@example.org>', 'phone': '+41\n79', 'source_badges': ['CRM', 'crm']}
    ws._contacts_store_path().write_text(json.dumps({'contacts': [raw]}))
    assert ws._read_manual_contacts()[0]['email'] == 'ada@example.org'
    ws._write_manual_contacts([raw])
    saved = json.loads(ws._contacts_store_path().read_text())['contacts'][0]
    assert saved['email'] == 'ada@example.org'
    assert saved['phone'] == '+41 79'
    assert saved['source_badges'] == ['CRM']


def test_gmail_numbered_search_produces_summary_and_query_contacts(ws, monkeypatch):
    # Wire format from google_workspace_mcp gmail_tools._format_gmail_results_plain.
    numbered = """Found 2 messages matching 'in:inbox is:unread':

📧 MESSAGES:
  1. Message ID: 18abc
     Web Link: https://mail.google.com/mail/u/0/#all/18abc
     Thread ID: thread-a
     Thread Link: https://mail.google.com/mail/u/0/#all/thread-a

  2. Message ID: 18def
     Web Link: https://mail.google.com/mail/u/0/#all/18def
     Thread ID: thread-b
     Thread Link: https://mail.google.com/mail/u/0/#all/thread-b

💡 USAGE:
  • Pass the Message IDs **as a list** to get_gmail_messages_content_batch()
    e.g. get_gmail_messages_content_batch(message_ids=[...])
  • Pass the Thread IDs to get_gmail_thread_content() (single) or get_gmail_threads_content_batch() (batch)"""
    metadata = [mail('18abc'), mail('18def', 'Bea <bea@example.org>')]
    calls = []

    def bridge(config, *, server, tool, params):
        calls.append((tool, params))
        if tool == 'search_gmail_messages':
            if params['query'] == 'in:inbox':
                return {'messages': [{'id': '18fed'}]}
            return {'result': {'content': [{'type': 'text', 'text': numbered}]}}
        assert tool == 'get_gmail_messages_content_batch'
        return {'messages': [item for item in metadata + [mail('18fed')] if item['id'] in params['message_ids']]}

    monkeypatch.setattr(ws, '_call_aiwerk_bridge_tool', bridge)
    monkeypatch.setattr(ws, '_ASSISTANT_EMAIL_PREVIEW_ITEMS', 3)
    monkeypatch.setattr(ws, '_contact_account_configs', lambda c: [{'user_google_email': 'me@example.org'}])
    result = ws._google_workspace_email_summary({}, {'backend': 'gmail', 'address': 'me@example.org'})
    contacts = ws._contacts_from_google_workspace_query_interactions({}, 'Ada', own_emails={'me@example.org'})
    assert [i['id'] for i in result['items']] == ['18abc', '18def', '18fed']
    assert [i['unread'] for i in result['items']] == [True, True, False]
    assert result['unread_count'] == 2
    assert [i['email'] for i in contacts] == ['ada@example.org', 'bea@example.org']
    assert [p['message_ids'] for tool, p in calls if tool == 'get_gmail_messages_content_batch'] == [
        ['18abc', '18def'], ['18fed'], ['18abc', '18def'],
    ]


def test_himalaya_structured_impersonators_do_not_consume_topup(ws, monkeypatch):
    monkeypatch.setattr(ws, '_ASSISTANT_EMAIL_PREVIEW_ITEMS', 5)
    unread = [{'id': 'U', 'from': {'name': 'Ada', 'addr': 'ada@example.org'}}]
    latest = [
        {'id': f'S{i}', 'from': {'name': 'Migros', 'addr': f'offer{i}@unrelated.example'}, 'subject': 'Hello'}
        for i in range(4)
    ] + [
        {'id': 'R1', 'from': {'name': 'Bea', 'addr': 'bea@example.org'}, 'flags': ['Seen']},
        {'id': 'R2', 'from': {'name': 'Cy', 'addr': 'cy@example.org'}, 'flags': ['Seen']},
    ]
    calls = []

    def envelopes(query=None, **kwargs):
        calls.append((query, kwargs))
        return unread if query else latest

    monkeypatch.setattr(ws, '_run_himalaya_envelope_list', envelopes)
    result = ws._merge_email_summaries([
        ws._himalaya_email_summary({}, {'backend': 'himalaya', 'account': 'main', 'folder': 'INBOX'}),
    ])
    assert [i['id'] for i in result['items']] == ['U', 'R1', 'R2']
    assert [i['unread'] for i in result['items']] == [True, False, False]
    assert result['unread_count'] == 1
    assert result['filtered_count'] == 4
    assert result['items'][1]['from'] == latest[4]['from']
    assert calls[0][0] == 'not flag Seen'
    assert calls[1] == (None, {'page_size': 6, 'account': 'main', 'folder': 'INBOX'})
    assert 'sender' not in latest[0]


def test_manual_contact_create_persists_and_returns_normalized_fields(ws):
    from starlette.testclient import TestClient

    ws._contacts_store_path().write_text(json.dumps({'contacts': [], 'hidden': ['keep-hidden']}))
    client = TestClient(ws.app)
    response = client.post('/api/cui/contacts', headers={ws._SESSION_HEADER_NAME: ws._SESSION_TOKEN}, json={
        'name': 'Ada', 'email': 'Ada <ADA@example.org>', 'phone': '+41\n79',
    })
    assert response.status_code == 200
    returned = response.json()['contact']
    saved = json.loads(ws._contacts_store_path().read_text())
    assert returned['email'] == 'ada@example.org'
    assert returned['phone'] == '+41 79'
    assert saved['contacts'] == [returned]
    assert saved['hidden'] == ['keep-hidden']


def test_contacts_summary_unions_badges_after_visibility_filtering(ws, monkeypatch):
    manual = [
        {'name': 'Ada', 'email': 'ada@example.org', 'source_badges': ['CRM']},
        {'name': 'Bea', 'email': 'bea@example.org', 'source_badges': ['CRM']},
        {'name': 'Hidden', 'email': 'hidden@example.org', 'key': 'hidden-key'},
        {'name': 'Me', 'email': 'me@example.org'},
    ]
    ws._contacts_store_path().write_text(json.dumps({'contacts': manual, 'hidden': ['hidden-key', 'hidden-copy']}))
    google = [
        {'name': 'Ada Google', 'email': 'ADA@example.org', 'source_badges': ['Google', 'crm']},
        {'name': 'Hidden Ada', 'email': 'ada@example.org', 'key': 'hidden-copy', 'source_badges': ['Private']},
        {'name': 'Newsletter', 'email': 'ada@example.org', 'source_badges': ['Unsafe']},
        {'name': 'Cy', 'email': 'cy@example.org', 'source_badges': ['Google']},
        {'name': 'Robot', 'email': 'noreply@example.org'},
    ]
    monkeypatch.setattr(ws, '_email_account_configs', lambda c: [{'address': 'me@example.org'}])
    monkeypatch.setattr(ws, '_calendar_accounts', lambda c: [])
    monkeypatch.setattr(ws, '_contact_account_configs', lambda c: [])
    monkeypatch.setattr(ws, '_contacts_from_google_workspace', lambda c, **kw: google)
    monkeypatch.setattr(ws, '_contacts_from_google_workspace_interactions', lambda *a: [])
    monkeypatch.setattr(ws, '_contacts_from_himalaya_interactions', lambda *a: [])
    result = ws._contacts_summary({}, {'accounts': []}, {})
    for field in ('relevant', 'frequent'):
        assert [i['email'] for i in result[field]] == ['ada@example.org', 'bea@example.org', 'cy@example.org']
        assert result[field][0]['name'] == 'Ada'
        assert result[field][0]['source_badges'] == ['CRM', 'Google']
    assert result['total_count'] == 3
