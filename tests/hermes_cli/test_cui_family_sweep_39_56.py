"""Contact producer sweep; transport-only doubles, exact-base RED fixture."""
import pytest
from tests.hermes_cli.test_cui_family_sweep import ws


@pytest.mark.parametrize('organizer,expected', [
    ({'emailAddress':{'name':' Ada ', 'address':' ada@example.org '}}, 'Ada <ada@example.org>'),
    ({'emailAddress':{'address':'ada@example.org'}}, 'ada@example.org'),
    ({'emailAddress':{'name':'Ada'}}, 'Ada'),
    ({'emailAddress':'broken'}, ''),
    (None, ''),
])
def test_microsoft_calendar_organizer_projection(ws, monkeypatch, organizer, expected):
    import json, html
    from tests.hermes_cli.test_cui_family_sweep import request
    quiet_sources(ws,monkeypatch)
    cfg={'calendar':{'accounts':[{'backend':'microsoft_calendar','address':'owner@example.org'}]}}
    event={'id':'event','subject':'Meeting','organizer':organizer}
    bridge=lambda value:{'content':[{'type':'text','text':json.dumps(value)}]}
    monkeypatch.setattr(ws,'_call_aiwerk_bridge_tool',lambda *a,**k:bridge({'value':[event]}))
    out=ws._calendar_summary(cfg)
    assert out['accounts'][0]['items'][0].get('organizer')==expected
    assert out['items'][0].get('organizer')==expected
    contacts=ws._contacts_summary(cfg,{},out)
    people=contacts.get('relevant',[])+contacts.get('frequent',[])
    if 'ada@example.org' in expected:
        person=next(c for c in people if c['email']=='ada@example.org')
        assert 'Aus Kalender' in person['source_badges'] and person['interaction_score']==2
    else: assert not people
    monkeypatch.setattr(ws,'load_config',lambda:cfg)
    monkeypatch.setattr(ws,'_assistant_resources_payload',lambda *a,**k:{'calendar':out})
    monkeypatch.setattr(ws,'_fetch_microsoft_calendar_event_detail',lambda *a:bridge({'organizer':{'emailAddress':{'name':'Grace','address':'grace@example.org'}}}))
    response=ws.view_assistant_calendar_event(request(ws),'owner@example.org','event')
    assert 'Grace &lt;grace@example.org&gt;' in response.body.decode()
    assert response.headers['cache-control']=='no-store'
    monkeypatch.setattr(ws,'_fetch_microsoft_calendar_event_detail',lambda *a:bridge({'organizer':None}))
    response=ws.view_assistant_calendar_event(request(ws),'owner@example.org','event')
    if expected: assert html.escape(expected) in response.body.decode()


def quiet_sources(ws, monkeypatch):
    for name in ('_contacts_from_google_workspace', '_contacts_from_google_workspace_interactions', '_contacts_from_himalaya_interactions'):
        monkeypatch.setattr(ws, name, lambda *a, **k: [])


def test_s8_resource_people_metadata_partition(ws, monkeypatch):
    quiet_sources(ws, monkeypatch)
    ws._write_contacts_store_payload({'contacts':[{'email':'saved@example.org'}, {'email':'noted@example.org','note':'Call'}]})
    mail={'accounts':[None, {'address':'me@example.org','items':[None, {'sender':'Ada <ada@example.org>','received_at':'2026-09-11T12:00:00Z'}]}]}
    cal={'accounts':[{'items':[{'organizer':'Org <org@example.org>','creator':'creator@example.org','email':'event@example.org','attendees':['guest@example.org', {'email':'me@example.org'}, {'address':'dict@example.org','display_name':'Dict'}, None, {'email':'root@example.org'}]}]}]}
    out=ws._contacts_summary({},mail,cal)
    by={c['email']:c for c in out['relevant']}
    assert {'ada@example.org','org@example.org','creator@example.org','event@example.org','guest@example.org','dict@example.org','noted@example.org'} <= set(by)
    assert by['ada@example.org']['interaction_score']==4
    assert by['ada@example.org']['last_interaction_at']=='2026-09-11T12:00:00Z'
    assert by['org@example.org']['interaction_score']==2
    assert [c['email'] for c in out['frequent']]==['saved@example.org']
    assert not {'me@example.org','root@example.org'} & {c['email'] for c in out['items']}


def test_s8_google_multivalue_provenance(ws, monkeypatch):
    monkeypatch.setattr(ws,'_contact_account_configs',lambda c:[{'user_google_email':'me@example.org','label':'Work'}])
    text='Contact ID: abc\nName: Ada\nEmail: ada@example.org\nEmail: other@example.org\nEmail: invalid\nPhone: +41 123 (work)\nPhone: +41 456\nPhone: invalid\nOrganization: Director at Example\n'
    monkeypatch.setattr(ws,'_call_aiwerk_bridge_tool',lambda *a,**k:{'content':[{'type':'text','text':text}]})
    c=ws._normalize_contact_item(ws._contacts_from_google_workspace({})[0])
    assert c['email']=='ada@example.org'
    assert c['emails']==['ada@example.org','other@example.org']
    assert c['phones']==['+41 123','+41 456']
    assert (c['role'],c['organization'])==('Director','Example')
    assert {'Google Contacts','Work'} <= set(c['source_badges'])


@pytest.mark.parametrize('mode',['gmail','query','himalaya'])
def test_s8_producers_aggregate_and_rank(ws, monkeypatch, mode):
    monkeypatch.setattr(ws,'_contact_account_configs',lambda c:[{'user_google_email':'me@example.org'}])
    monkeypatch.setattr(ws,'_himalaya_contact_accounts',lambda c:[{'account':'work'}])
    rows=[{'from':'me@example.org','to':'weak@example.org'}, {'from':'me@example.org','to':'strong@example.org','date':'2999-01-01T00:00:00Z'}, {'from':'me@example.org','to':'strong@example.org','date':'2999-01-02T00:00:00Z'}]
    monkeypatch.setattr(ws,'_gmail_bridge_search_message_ids',lambda *a,**k:['x'] if (mode=='query' or a[1].startswith('in:sent')) else [])
    monkeypatch.setattr(ws,'_gmail_bridge_metadata_items_for_ids',lambda *a,**k:rows)
    monkeypatch.setattr(ws,'_run_himalaya_envelope_list',lambda **k:rows if k['folder']=='Sent' else [])
    got=(ws._contacts_from_google_workspace_query_interactions({},'mixed',own_emails={'me@example.org'}) if mode=='query' else ws._contacts_from_google_workspace_interactions({}, {'me@example.org'}) if mode=='gmail' else ws._contacts_from_himalaya_interactions({}, {'me@example.org'}))
    assert [c['email'] for c in got]==['strong@example.org','weak@example.org']
    assert got[0]['interaction_count']==2 and got[0]['interaction_score']==10
    assert got[0]['last_interaction_at']=='2999-01-02T00:00:00Z'


@pytest.mark.parametrize('signals',[19,20])
def test_s8_signal_first_budget_counters_privacy(ws, monkeypatch, signals):
    quiet_sources(ws,monkeypatch)
    monkeypatch.setenv('AIWERK_CUI_CONTACTS_SAVED_TOP_UP_TARGET','20')
    monkeypatch.setenv('AIWERK_CUI_CONTACTS_RELEVANCE_WINDOW_DAYS','7')
    rows=[{'email':f'human{i}@example.org','interaction_count':1,'interaction_score':i+1,'relevance':'relevant'} for i in range(signals)]
    monkeypatch.setattr(ws,'_contacts_from_google_workspace_interactions',lambda *a:rows)
    saved=[{'email':'human0@example.org','organization':'Company','source_badges':['Google Contacts','me@example.org']}, {'email':'hidden@example.org','source_badges':['Google Contacts']}, {'email':'me@example.org'}, *[{'email':f'saved{i}@example.org','source_badges':['Google Contacts']} for i in range(20)]]
    monkeypatch.setattr(ws,'_contacts_from_google_workspace',lambda *a,**k:saved)
    ws._write_hidden_contact_keys(['hidden@example.org'])
    out=ws._contacts_summary({}, {'accounts':[{'address':'me@example.org'}]}, {})
    assert out['total_count']==20
    assert out['saved_count']==20-signals
    assert out['relevance_window_days']==7 and out['saved_top_up_target']==20
    assert out['relevant'][0]['email']==f'human{signals-1}@example.org'
    assert next(c for c in out['relevant'] if c['email']=='human0@example.org')['organization']=='Company'
    assert not set(c['email'] for c in out['relevant']) & set(c['email'] for c in out['frequent'])
    assert all('me@example.org' not in c['source_badges'] for c in out['relevant']+out['frequent'])


@pytest.mark.parametrize('source',['manual','google','gmail','himalaya','calendar','mail'])
def test_s8_whole_source_long_id_hide(ws, monkeypatch, source):
    from tests.hermes_cli.test_cui_family_sweep import request
    quiet_sources(ws,monkeypatch)
    raw={'email':'long@example.org','display_name':'Long '+('X'*110),'key':'PERSIST'}
    mail={}; cal={}
    if source=='manual': ws._write_contacts_store_payload({'contacts':[raw]})
    elif source in {'google','gmail','himalaya'}:
        name={'google':'_contacts_from_google_workspace','gmail':'_contacts_from_google_workspace_interactions','himalaya':'_contacts_from_himalaya_interactions'}[source]
        monkeypatch.setattr(ws,name,lambda *a,**k:[raw])
    else:
        resource={'accounts':[{'items':[{'sender':raw['display_name']+' <long@example.org>','organizer':raw['display_name']+' <long@example.org>'}]}]}
        if source=='mail': mail=resource
        else: cal=resource
    first=ws._contacts_summary({},mail,cal)
    c=(first['relevant']+first['frequent'])[0]
    ws.hide_cui_contact(request(ws),ws.CuiContactHideRequest(id=c['id']))
    after=ws._contacts_summary({},mail,cal)
    assert after['relevant']+after['frequent']==[]
    monkeypatch.setattr(ws,'_assistant_resources_payload',lambda **k:{'contacts':first,'email':mail,'calendar':cal})
    assert ws._search_contacts_payload()['items']==[]
    if source=='manual': assert ws._read_contacts_store_payload()['contacts'][0]['key']=='PERSIST'



def test_s8_positive_signal_selection_ranking_and_hidden_badges(ws, monkeypatch):
    quiet_sources(ws, monkeypatch)
    ws._write_contacts_store_payload({'contacts':[{'email':'ada@example.org','source_badges':['CRM']}], 'hidden':['hidden-copy']})
    monkeypatch.setattr(ws,'_contacts_from_google_workspace',lambda *a,**k:[{'email':'ada@example.org','source_badges':['Google']}, {'email':'ada@example.org','key':'hidden-copy','source_badges':['Private']}, {'email':'ada@example.org','name':'Newsletter','source_badges':['Unsafe']}])
    out=ws._contacts_summary({}, {}, {'accounts':[{'items':[{'account_address':'ada@example.org'}]}]})
    assert out['frequent']==[]
    assert out['relevant'][0]['relevance']=='related'
    assert out['relevant'][0]['source_badges']==['CRM','Google']
    rows=[ws._normalize_contact_item({'email':f'{name.lower()}@example.org','display_name':name,'interaction_score':score,'interaction_count':count,'last_interaction_at':date}) for name,score,count,date in [('A',4,1,'2026-01-01'),('B',4,1,'2026-01-01'),('C',4,2,'2025-01-01'),('D',5,1,'2024-01-01'),('E',4,1,'2026-02-01')]]
    assert [c['display_name'] for c in ws._rank_contacts(rows)]==['D','C','E','B','A']
    text='Contact ID: bound\n'+''.join(f'Email: person{i}@example.org\nPhone: +41 {i}\n' for i in range(8))
    parsed=ws._parse_google_contacts(text)[0]
    assert len(parsed['emails'])==len(parsed['phones'])==5


def test_contacts_env_file_and_store_union(ws, monkeypatch, tmp_path):
    import json
    ws._write_contacts_store_payload({'contacts':[{'email':'stored@example.org'}]})
    file=tmp_path/'input.json'
    file.write_text(json.dumps({'items':[{'email':'file@example.org'}]}))
    monkeypatch.setenv('AIWERK_CUI_CONTACTS_JSON',str(file))
    assert {c['email'] for c in ws._read_manual_contacts()}=={'file@example.org','stored@example.org'}
    monkeypatch.setenv('AIWERK_CUI_CONTACTS_JSON','[{"email":"inline@example.org"}]')
    assert {c['email'] for c in ws._read_manual_contacts()}=={'inline@example.org','stored@example.org'}
    for bad in ['missing-file','{broken']:
        monkeypatch.setenv('AIWERK_CUI_CONTACTS_JSON',bad)
        assert [c['email'] for c in ws._read_manual_contacts()]==['stored@example.org']
    file.write_text(' '*256001)
    monkeypatch.setenv('AIWERK_CUI_CONTACTS_JSON',str(file))
    assert [c['email'] for c in ws._read_manual_contacts()]==['stored@example.org']


def test_himalaya_envelopes_window_direction_structured_addresses(ws, monkeypatch):
    monkeypatch.setattr(ws,'_himalaya_contact_accounts',lambda c:[{'account':'work'}])
    monkeypatch.setattr(ws,'_himalaya_contact_folder',lambda a,sent:'Sent' if sent else 'INBOX')
    monkeypatch.setenv('AIWERK_CUI_CONTACTS_RELEVANCE_WINDOW_DAYS','10')
    def envelopes(**kw):
        if kw['folder']=='Sent':
            return [{'date':'2999-01-01 02:00:00+02:00','from':{'addr':'me@example.org'},'to':[{'name':'Ada','addr':'ada@example.org'}], 'bcc':[{'name':'Grace','email':'grace@example.org'}]}]
        return [{'date':'2999-01-01T00:00:00Z','from':{'name':'Lin','addr':'lin@example.org'},'to':{'addr':'bystander@example.org'}}, {'date':'2000-01-01T00:00:00Z','from':'old@example.org'}, {'date':'invalid','from':'unknown@example.org'}]
    monkeypatch.setattr(ws,'_run_himalaya_envelope_list',envelopes)
    contacts=ws._contacts_from_himalaya_interactions({}, {'me@example.org'})
    by={c['email']:c for c in contacts}
    assert set(by)=={'ada@example.org','grace@example.org','lin@example.org','unknown@example.org'}
    assert by['ada@example.org']['display_name']=='Ada'
    assert by['ada@example.org']['interaction_score']==5
    assert by['ada@example.org']['last_interaction_at']=='2999-01-01T00:00:00Z'
    assert by['lin@example.org']['interaction_score']==4
    assert by['lin@example.org']['relevance']=='relevant'
    assert by['unknown@example.org']['last_interaction_at']=='invalid'


@pytest.mark.parametrize('query_mode',[False,True])
def test_gmail_interactions_direction_bulk_and_metadata(ws, monkeypatch, query_mode):
    monkeypatch.setattr(ws,'_contact_account_configs',lambda c:[{'user_google_email':'me@example.org'}])
    monkeypatch.setattr(ws,'_gmail_bridge_search_message_ids',lambda *a,**k:['mail'])
    calls=[]
    def metadata(*a,**k):
        calls.append(1)
        sent={'from':'me@example.org','to':'Ada <ada@example.org>','bcc':'Grace <grace@example.org>', 'date':'2026-09-11T10:00:00Z','label_ids':['SENT'],'precedence':'bulk'}
        inbox=[{'from':'human@example.org','to':'bystander@example.org','date':'2026-09-11T09:00:00Z','auto_submitted':'no'}, {'from':'ordinary@example.org','list_unsubscribe':'https://example.org/unsubscribe'}]
        return [sent,*inbox] if query_mode else ([sent] if len(calls)==1 else inbox)
    monkeypatch.setattr(ws,'_gmail_bridge_metadata_items_for_ids',metadata)
    contacts=(ws._contacts_from_google_workspace_query_interactions({},'mixed',own_emails={'me@example.org'}) if query_mode else ws._contacts_from_google_workspace_interactions({}, {'me@example.org'}))
    by={c['email']:c for c in contacts}
    assert set(by)=={'ada@example.org','grace@example.org','human@example.org'}
    assert by['ada@example.org']['interaction_score']==5
    assert by['human@example.org']['interaction_score']==4
    assert by['ada@example.org']['last_interaction_at']=='2026-09-11T10:00:00Z'


def test_interaction_contact_ranking_merges_metadata(ws):
    raw=[{'email':'ada@example.org','interaction_score':2,'interaction_count':1,'source_badges':['Mail']}, {'email':'ada@example.org','interaction_score':5,'interaction_count':2,'source_badges':['Calendar'],'organization':'Company','last_interaction_at':'2026-09-11'}]
    got=ws._dedupe_contacts(raw)
    assert len(got)==1
    assert got[0]['interaction_score']==7
    assert got[0]['interaction_count']==3
    assert got[0]['organization']=='Company'
    assert got[0]['last_interaction_at']=='2026-09-11'
    assert got[0]['source_badges']==['Mail','Calendar']
    assert raw[0]['interaction_score']==2


@pytest.mark.parametrize('header',['Precedence: bulk','Precedence: junk','Precedence: list','List-Unsubscribe: https://example.org/unsub','Auto-Submitted: auto-generated'])
def test_gmail_bulk_real_metadata_parser_positive_control(ws, monkeypatch, header):
    monkeypatch.setattr(ws,'_contact_account_configs',lambda c:[{'user_google_email':'me@example.org'}])
    def bridge(config, *, tool, **kw):
        if tool=='search_gmail_messages':
            return {'content':[{'type':'text','text':'1. Message ID: bulk\n2. Message ID: human\n3. Message ID: sent'}]}
        return {'content':[{'type':'text','text':f'Message ID: bulk\nFrom: ordinary@example.org\n{header}\nMessage ID: human\nFrom: human@example.org\nAuto-Submitted: no\nDate: 2026-09-11\nMessage ID: sent\nFrom: me@example.org\nTo: recipient@example.org\n{header}'}]}
    monkeypatch.setattr(ws,'_call_aiwerk_bridge_tool',bridge)
    got=ws._contacts_from_google_workspace_query_interactions({},'mixed',own_emails={'me@example.org'})
    assert {c['email'] for c in got}=={'human@example.org','recipient@example.org'}


def test_contact_source_union_id_only_hide_positive_control(ws, monkeypatch, tmp_path):
    from tests.hermes_cli.test_cui_family_sweep import request
    monkeypatch.setenv('AIWERK_CUI_CONTACTS_JSON','[{"display_name":"Ada","email":"ada@example.org","key":"PERSIST"}]')
    for name in ('_contacts_from_google_workspace','_contacts_from_google_workspace_interactions','_contacts_from_himalaya_interactions'):
        monkeypatch.setattr(ws,name,lambda *a,**k:[])
    # Same row44 visibility-only correction as batch1; hide remains mandatory.
    before=ws._contacts_summary({}, {}, {})
    first=(before['relevant']+before['frequent'])[0]
    assert first['key']=='PERSIST'
    ws.hide_cui_contact(request(ws),ws.CuiContactHideRequest(id=first['id']))
    after=ws._contacts_summary({}, {}, {})
    assert after['relevant']+after['frequent']==[]
    monkeypatch.setattr(ws,'_assistant_resources_payload',lambda **kw:{'contacts':{'items':[first]},'email':{},'calendar':{}})
    monkeypatch.setattr(ws,'_contacts_from_google_workspace_query_interactions',lambda *a,**kw:[])
    assert ws._search_contacts_payload()['items']==[]


@pytest.mark.parametrize('raw,expected', [('2026-09-01 12:30+02:00','2026-09-01T10:30:00Z'), ('2026-09-01T10:30:00','2026-09-01T10:30:00Z'), (' broken ','broken'), ('',None), (None,None), (42,None)])
def test_email_rows_shared_himalaya_date_parser(ws,monkeypatch,raw,expected):
    import copy
    parser=getattr(ws,'_parse_himalaya_email_date',None)
    assert callable(parser), 'One shared Himalaya date owner is required'
    assert parser(raw)==expected
    envelope={'id':'1','date':raw,'from':{'name':'Ada','addr':'ada@example.org'}}
    old=copy.deepcopy(envelope)
    monkeypatch.setattr(ws,'_run_himalaya_envelope_list',lambda **kw:[envelope])
    mail=ws._himalaya_email_summary({}, {'backend':'himalaya','account':'chosen'})
    assert mail['items'][0]['received_at']==expected
    monkeypatch.setattr(ws,'_himalaya_contact_accounts',lambda *a:[{'account':'chosen'}])
    monkeypatch.setattr(ws,'_contacts_relevance_window_days',lambda:365000)
    contacts=ws._contacts_from_himalaya_interactions({},set())
    assert contacts and contacts[0].get('last_interaction_at','')==(expected or '')
    assert envelope==old
    # Both live consumers must route through the same owner, not duplicate parsers.
    seen=[]
    monkeypatch.setattr(ws,'_parse_himalaya_email_date',lambda value:seen.append(value) or parser(value))
    ws._himalaya_email_summary({}, {'backend':'himalaya','account':'chosen'})
    mail_calls=len(seen)
    ws._contacts_from_himalaya_interactions({},set())
    assert mail_calls>0 and len(seen)>mail_calls


@pytest.fixture
def security_shared(ws, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    root = tmp_path / 'Shared'
    root.mkdir()
    monkeypatch.setattr(ws, '_resolve_shared_folder_root', lambda *a: root)
    monkeypatch.setattr(ws.hermes_constants, 'get_hermes_home', lambda: tmp_path)
    monkeypatch.setattr(ws, '_SESSION_TOKEN', 'synthetic-security-token')
    return root, TestClient(ws.app), {'Authorization': 'Bearer synthetic-security-token'}


@pytest.mark.parametrize('relative', ['innocuous.txt', 'nested/innocuous.txt', 'alias/visible.txt', '.hidden/visible.txt', 'passwords/visible.txt', 'escape/visible.txt'])
@pytest.mark.parametrize('consumer', ['get', 'attachment'])
def test_security_shared_resolved_hidden_and_escape_denied(ws, security_shared, tmp_path, relative, consumer):
    root, client, headers = security_shared
    (root / 'passwords.txt').write_text('SYNTHETIC-HIDDEN')
    (root / 'innocuous.txt').symlink_to(root / 'passwords.txt')
    (root / 'nested').mkdir()
    (root / 'nested/innocuous.txt').symlink_to(root / 'passwords.txt')
    for name in ['passwords', '.hidden']:
        (root / name).mkdir()
        (root / name / 'visible.txt').write_text('SYNTHETIC-HIDDEN')
    (root / 'alias').symlink_to(root / 'passwords', target_is_directory=True)
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'visible.txt').write_text('SYNTHETIC-OUTSIDE')
    (root / 'escape').symlink_to(outside, target_is_directory=True)
    if consumer == 'get':
        response = client.get('/api/assistant/shared-folder/open', params={'path': relative}, headers=headers)
    else:
        response = client.post('/api/assistant/attachments/resource', json={'kind': 'shared_file', 'item': {'open_url': '/api/assistant/shared-folder/open?path=' + relative}}, headers=headers)
    expected = 400 if consumer == 'attachment' and relative.startswith('.') else 404
    assert response.status_code == expected, response.text
    assert 'SYNTHETIC-HIDDEN' not in response.text and 'SYNTHETIC-OUTSIDE' not in response.text
    assert not (tmp_path / 'dashboard_uploads').exists()


def test_security_shared_visible_download_attachment_and_token(ws, security_shared):
    from pathlib import Path
    root, client, headers = security_shared
    (root / 'folder').mkdir()
    (root / 'folder/visible.txt').write_text('ordinary visible content')
    (root / 'alias.txt').symlink_to(root / 'folder/visible.txt')
    for relative in ['folder/visible.txt', 'alias.txt']:
        response = client.get('/api/assistant/shared-folder/open', params={'path': relative}, headers=headers)
        assert response.status_code == 200 and response.text == 'ordinary visible content'
        assert response.headers['x-content-type-options'] == 'nosniff'
        payload = {'kind': 'shared_file', 'item': {'open_url': '/api/assistant/shared-folder/open?path=' + relative}}
        response = client.post('/api/assistant/attachments/resource', json=payload, headers=headers)
        assert response.status_code == 200, response.text
        assert Path(response.json()['attachments'][0]['path']).read_text() == 'ordinary visible content'
        assert client.post('/api/assistant/attachments/resource', json=payload).status_code == 401
        assert client.get('/api/assistant/shared-folder/open', params={'path': relative}).status_code == 401


def test_security_shared_listing_confinement(ws, security_shared, tmp_path, monkeypatch):
    import json
    from pathlib import Path
    root, _, _ = security_shared
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'external-name.txt').write_text('outside')
    (root / 'escape').symlink_to(outside, target_is_directory=True)
    (root / 'passwords.txt').write_text('hidden')
    (root / 'alias.txt').symlink_to(root / 'passwords.txt')
    (root / 'folder').mkdir()
    (root / 'folder/visible.txt').write_text('visible')
    (root / 'folder/escape').symlink_to(outside, target_is_directory=True)
    (root / 'folder/.hidden').mkdir()
    (root / 'folder/.hidden/hidden-name.txt').write_text('hidden')
    real_iterdir = Path.iterdir
    visited = []
    def iterdir(path):
        visited.append(path.resolve())
        return real_iterdir(path)
    monkeypatch.setattr(Path, 'iterdir', iterdir)
    out = ws._shared_folder_summary({})
    text = json.dumps(out)
    assert outside not in visited, 'must reject escape before traversal'
    assert all(name not in text for name in ['external-name', 'escape', 'alias.txt', 'passwords', 'hidden-name'])
    assert 'visible.txt' in text


@pytest.mark.parametrize('fault', ['none', 'stat', 'iterdir'])
def test_security_shared_listing_metadata_and_bad_entry(ws, security_shared, monkeypatch, fault):
    import os
    from pathlib import Path
    root, _, _ = security_shared
    (root / 'folder').mkdir()
    visible = root / 'folder/visible.txt'
    visible.write_text('hello')
    os.utime(visible, (0, 0))
    bad = root / 'bad.txt'
    if fault == 'stat': bad.write_text('bad')
    if fault == 'iterdir':
        bad.mkdir()
        original_iterdir = Path.iterdir
        def iterdir(path):
            if path == bad: raise OSError('synthetic directory failure')
            return original_iterdir(path)
        monkeypatch.setattr(Path, 'iterdir', iterdir)
    real_stat = Path.stat
    def stat(path, *a, **kw):
        if path == bad and fault == 'stat': raise OSError('synthetic stat failure')
        return real_stat(path, *a, **kw)
    monkeypatch.setattr(Path, 'stat', stat)
    items = ws._shared_folder_summary({})['items']
    assert [i['name'] for i in items] == ['folder']
    folder = items[0]
    assert folder['mime'] is None and folder['size_bytes'] is None and folder['child_count'] == 1
    item = folder['children'][0]
    assert item['id'] == ws._safe_resource_id('folder/visible.txt')
    assert item['mime'] == 'text/plain' and item['size_bytes'] == 5
    assert item['modified_at'] == '1970-01-01T00:00:00Z'
    assert item['reference_uri'] == 'shared://folder/visible.txt'
    monkeypatch.setattr(ws, '_ASSISTANT_RESOURCE_MAX_SHARED_DEPTH', 0)
    assert 'children' not in ws._shared_folder_summary({})['items'][0]
    monkeypatch.setattr(ws, '_ASSISTANT_RESOURCE_MAX_SHARED_ITEMS', 0)
    assert ws._shared_folder_summary({})['items'] == []
