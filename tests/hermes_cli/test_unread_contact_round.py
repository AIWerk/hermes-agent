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
