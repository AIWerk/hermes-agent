"""Batch 1 contracts: map rows 1/6/8/9/12/38/52, authority paste_25.
External adapters and desktop launches are intercepted; storage is synthetic.
"""
import pytest

@pytest.mark.parametrize('mode',['default','config','env'])
def test_cheap_todo_path_and_roundtrip(ws,monkeypatch,tmp_path,mode):
    monkeypatch.delenv('AIWERK_CUI_TODO_PATH',raising=False)
    monkeypatch.setattr(ws.hermes_constants,'get_hermes_home',lambda:tmp_path)
    monkeypatch.setenv('HOME',str(tmp_path))
    cfg={'dashboard':{'todos':{'path':str(tmp_path/'configured.md')}}} if mode!='default' else {}
    expected=tmp_path/({'default':'TODO.md','config':'configured.md','env':'override.md'}[mode])
    if mode=='env': monkeypatch.setenv('AIWERK_CUI_TODO_PATH','~/override.md')
    monkeypatch.setattr(ws,'load_config',lambda:cfg)
    expected.write_text('- [ ] Existing <!-- hermes:id=stable status=pending -->\n')
    assert ws._todo_summary(cfg)['items'][0]['id']=='stable'
    ws._add_todo_item('New task')
    ws._write_todo_lines(ws._update_todo_item_done(ws._read_todo_lines(),'stable',True))
    assert ws._todo_summary(cfg)['done_count']==1
    assert 'New task' in expected.read_text() and 'id=stable' in expected.read_text()
    assert not (tmp_path/'assistant_todos.md').exists()

def test_cheap_todo_display_cleaning(ws):
    raw='`Code` [Label](https://example.org) <!-- hermes:internal=hidden -->   '+('Long ' * 1000)
    line='- [x] '+raw+' <!-- hermes:id=stable status=done -->'
    item=ws._todo_items([line])[0]
    assert item['text'].startswith('Code Label Long') and len(item['text'])==180
    assert len(item['full_text'])==4000 and 'hermes:' not in item['full_text']
    assert item['id']=='stable' and item['done'] is True
    assert ws._todo_items(ws._update_todo_item_done([line],'stable',False))[0]['id']=='stable'

@pytest.mark.parametrize('kind,item,expected', [
    ('calendar_event', {'title':'Planning','starts_at':'2026-10-01T10:00','ends_at':'2026-10-01T11:00','source':'work-calendar','location_hint':'Room 4','account_address':'work@example.org','html_link':'https://secret.example.org/event'}, ['Planning','2026-10-01T10:00','2026-10-01T11:00','work-calendar','Room 4','work@example.org','[LINK]']),
    ('contact', {'display_name':'Alice Human','organization':'Example Ltd','role':'Director','email':'alice@example.org','phone':'+41 555 999','source_badges':['Manual','From calendar']}, ['Alice Human','Example Ltd','Director','alice@example.org','+41 555 999','Manual','From calendar']),
])
def test_cheap_typed_attachment(ws,monkeypatch,tmp_path,kind,item,expected):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(ws.hermes_constants,'get_hermes_home',lambda:tmp_path)
    monkeypatch.setattr(ws,'_SESSION_TOKEN','secret')
    client=TestClient(ws.app)
    response=client.post('/api/assistant/attachments/resource',json={'kind':kind,'item':item},headers={'Authorization':'Bearer secret'})
    assert response.status_code==200, response.text
    from pathlib import Path
    text=Path(response.json()['attachments'][0]['path']).read_text()
    for value in expected: assert value in text
    assert 'https://secret.example.org' not in text
    assert client.post('/api/assistant/attachments/resource',json={'kind':kind,'item':item}).status_code==401

@pytest.mark.parametrize('backend', ['google_workspace','himalaya'])
def test_cheap_email_attachment_metadata_body(ws,monkeypatch,tmp_path,backend):
    from pathlib import Path
    acct={'backend':backend,'address':'chosen@example.org','account':'mailbox-name','folder':'Archive'}
    monkeypatch.setattr(ws,'load_config',lambda:{'email':{'accounts':[acct]}})
    monkeypatch.setattr(ws.hermes_constants,'get_hermes_home',lambda:tmp_path)
    metadata={'id':'msg','subject':'Trusted subject','sender':'Alice <alice@example.org>','received_at':'2026-09-10','source':'Trusted source'}
    monkeypatch.setattr(ws,'_assistant_resources_payload',lambda *a,**k:{'email':{'accounts':[None,{'address':'other@example.org','items':[{'id':'msg','subject':'Other private subject'}]},{'address':'CHOSEN@example.org','items':[None,metadata]}]}})
    calls=[]
    monkeypatch.setattr(ws,'_run_google_workspace_message_read',lambda c,a,message_id:calls.append((a,message_id)) or 'Real body https://secret.example.org')
    monkeypatch.setattr(ws,'_run_himalaya_message_read',lambda message_id,account,folder:calls.append((account,folder,message_id)) or 'Real body https://secret.example.org')
    from fastapi.testclient import TestClient
    client=TestClient(ws.app)
    response=client.post('/api/assistant/attachments/resource',json={'kind':'email','item':{'account_address':'chosen@example.org','message_id':'msg','subject':'Forged subject'}},headers={'Authorization':'Bearer '+ws._SESSION_TOKEN})
    assert response.status_code==200, response.text
    text=Path(response.json()['attachments'][0]['path']).read_text()
    for value in ['Trusted subject','Alice','2026-09-10','Real body','Trusted source']: assert value in text
    assert 'Other private subject' not in text and 'Forged subject' not in text and 'https://secret.example.org' not in text
    assert calls==([(acct,'msg')] if backend=='google_workspace' else [('mailbox-name','Archive','msg')])
    reader=ws.view_assistant_email(request(ws),'chosen@example.org','msg')
    assert b'Trusted subject' in reader.body and b'Alice' in reader.body

@pytest.mark.parametrize('failure,status', [('missing',400),('unknown',404),('binary',503),('read',502),('invalid',400)])
def test_cheap_email_attachment_fails_before_write(ws,monkeypatch,tmp_path,failure,status):
    monkeypatch.setattr(ws.hermes_constants,'get_hermes_home',lambda:tmp_path)
    monkeypatch.setattr(ws,'load_config',lambda:{'email':{'accounts':[{'backend':'himalaya','address':'chosen@example.org'}]}})
    monkeypatch.setattr(ws,'_assistant_resources_payload',lambda *a,**kw:{})
    def read(*a,**kw): raise {'binary':FileNotFoundError,'invalid':ValueError}.get(failure,RuntimeError)('failed')
    monkeypatch.setattr(ws,'_run_himalaya_message_read',read)
    item={'account_address':'other@example.org' if failure=='unknown' else 'chosen@example.org','message_id':'' if failure=='missing' else 'msg'}
    with pytest.raises(ws.HTTPException) as exc: ws.attach_assistant_resource(request(ws),{'kind':'email','item':item})
    assert exc.value.status_code==status
    assert not (tmp_path/'dashboard_uploads').exists()

@pytest.mark.parametrize('name', ['email','calendar','contacts','shared_folder','vault'])
def test_cheap_cold_initial_payload(ws, monkeypatch, name):
    ws._ASSISTANT_RESOURCE_CACHE.clear()
    ws._ASSISTANT_RESOURCE_CACHE_GENERATIONS.clear()
    scheduled=[]
    calls=[]
    monkeypatch.setattr(ws, '_assistant_schedule_resource_refresh', lambda key,builder,ttl,**kw: scheduled.append((key,builder,ttl)) or True)
    monkeypatch.setattr(ws, '_resource_payload', lambda resource,*a: calls.append(resource) or {'status':'connected','items':[]})
    monkeypatch.setattr(ws, '_connector_summary', lambda *a,**kw: [])
    out=ws._assistant_resources_payload()
    assert out[name]['status']=='loading'
    assert out[name]['refreshing'] is True and name not in calls
    if name in ('email','calendar'): assert out[name]['items']==[] and out[name]['accounts']==[]
    if name=='email': assert out[name]['unread_count']==0
    if name=='contacts':
        for key in ('items','manual','frequent','relevant'): assert out[name][key]==[]
        for key in ('total_count','manual_count','connected_count','interaction_count','saved_count','saved_top_up_target'): assert out[name][key]==0
        assert out[name]['relevance_window_days']==10 and out[name]['source_label']=='Relevante Kontakte'
    if name=='shared_folder': assert out[name]['root_label']=='Shared' and out[name]['total_count']==0
    if name=='vault': assert out[name]['compromised_count'] is None
    key,builder,ttl=next(entry for entry in scheduled if entry[0].startswith(name+':'))
    ws._assistant_write_resource_cache(key,builder(),ttl)
    assert ws._assistant_resources_payload()[name]['status']=='connected'
    for forced,target in ((True,None),(False,name)):
        ws._ASSISTANT_RESOURCE_CACHE.clear()
        calls.clear()
        assert ws._assistant_resources_payload(force_refresh=forced,refresh_resource=target)[name]['status']=='connected'
        assert name in calls

@pytest.mark.parametrize('path', ['normal', 'merge', 'env'])
@pytest.mark.parametrize('item,expected_account,expected_id', [
    ({'id':'wrong', 'event_id':' event /+? ', 'account_address':'person+tag@example.org', 'open_url':''}, 'person+tag@example.org', 'event /+?'),
    ({'id':'event', 'account_label':'Item Calendar'}, 'Item Calendar', 'event'),
    ({'id':'event'}, 'Kalender', 'event'),
    ({'id':'', 'event_id':'   '}, None, None),
    ({'id':'event', 'open_url':'https://example.org/kept'}, None, 'kept'),
])
def test_calendar_open_url_contract_all_sources(ws, monkeypatch, path, item, expected_account, expected_id):
    import copy, json
    from urllib.parse import urlsplit, parse_qs
    item=copy.deepcopy(item)
    original=copy.deepcopy(item)
    summary={'status':'connected','items':[item]}
    if path=='normal':
        monkeypatch.setattr(ws,'_google_workspace_calendar_summary',lambda *a:summary)
        out=ws._calendar_summary({'calendar':{'accounts':[{'backend':'google_workspace','address':'configured@example.org'}]}})
    elif path=='merge': out=ws._merge_calendar_summaries([summary])
    else:
        monkeypatch.setenv('AIWERK_CUI_CALENDAR_SUMMARY_JSON',json.dumps({'items':[item],'accounts':[summary]}))
        out=ws._calendar_summary({})
    for got in (out['items'][0],out['accounts'][0]['items'][0]):
        if expected_id=='kept': assert got['open_url']=='https://example.org/kept'
        elif expected_id is None: assert not got.get('open_url')
        else:
            url=urlsplit(got.get('open_url',''))
            assert url.path=='/api/assistant/calendar/view'
            assert parse_qs(url.query)=={'account':[expected_account],'id':[expected_id]}
    assert item==original


def test_calendar_producer_view_event_ref_and_auth(ws, monkeypatch):
    from urllib.parse import urlsplit,parse_qs
    cfg={'calendar':{'accounts':[{'backend':'microsoft_calendar','address':'owner@example.org'}]}}
    summary={'address':'owner@example.org','status':'connected','items':[{'id':'legacy','event_id':'event','title':'Calendar title','source':'microsoft_calendar'}]}
    out=ws._merge_calendar_summaries([summary])
    monkeypatch.setattr(ws,'load_config',lambda:cfg)
    monkeypatch.setattr(ws,'_assistant_resources_payload',lambda *a,**k:{'calendar':out})
    calls=[]
    monkeypatch.setattr(ws,'_fetch_microsoft_calendar_event_detail',lambda c,a,i:calls.append((a,i)) or {})
    query=parse_qs(urlsplit(out['items'][0].get('open_url','')).query)
    assert query=={'account':['owner@example.org'],'id':['event']}
    response=ws.view_assistant_calendar_event(request(ws),query['account'][0],query['id'][0])
    assert b'Calendar title' in response.body and calls==[(cfg['calendar']['accounts'][0],'event')]
    for account,authorized,status in [('foreign@example.org',True,404),('owner@example.org',False,401)]:
        with pytest.raises(ws.HTTPException) as exc: ws.view_assistant_calendar_event(request(ws,authorized=authorized),account,'event')
        assert exc.value.status_code==status
    assert len(calls)==1
    assert ws._calendar_account_config_for_ref(cfg,'foreign@example.org')=={}
from starlette.requests import Request

@pytest.fixture
def ws(monkeypatch, tmp_path):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.delenv('HERMES_CUI_ALLOW_REMOTE_FILE_MANAGER_OPEN', raising=False)
    from hermes_cli import web_server as ws
    if __import__('os').environ.get('CUI_SWEEP_BASE_RED'):
        import subprocess
        import types
        import sys
        source = subprocess.check_output(['git', 'show', '963ce27c207daf6230d4e4e66c20c293a23280a9:hermes_cli/web_server.py'], text=True)
        import hashlib
        digest = hashlib.sha256(source.encode()).hexdigest()
        assert digest == '95a0e2d78d74a4e1d89b824400ff337e9d6cf1a9815a504d8b7b2a0aae6483bf'
        print('CUI_SWEEP_BASE_SHA256=' + digest)
        baseline = types.ModuleType('hermes_cli._batch1_baseline')
        baseline.__file__ = ws.__file__
        baseline.__package__ = 'hermes_cli'
        monkeypatch.setitem(sys.modules, baseline.__name__, baseline)
        exec(compile(source, baseline.__file__, 'exec'), baseline.__dict__)
        ws = baseline
    monkeypatch.setattr(ws, 'load_config', lambda: {})
    return ws

@pytest.mark.parametrize('kind,alias', [('calendar','calendars'),('email','mailbox')])
@pytest.mark.parametrize('outer', ['assistant','dashboard','root'])
def test_s3_sections_shapes_defaults(ws, kind, alias, outer):
    section={'google_workspace':{'server':'chosen','accounts':[{'address':'yes@example.org'}, {'address':'off@example.org','enabled':False}]}}
    cfg={alias:section} if outer=='root' else {outer:{alias:section}}
    cfg[kind]={'accounts':[{'address':'wrong@example.org','backend':'google_workspace'}]} if outer!='root' else section
    accounts=ws._calendar_accounts(cfg) if kind=='calendar' else ws._email_account_configs(cfg)
    assert [a['address'] for a in accounts]==['yes@example.org']
    assert accounts[0]['server']=='chosen' and accounts[0]['backend']=='google_workspace'
    assert ws._calendar_account_config_for_ref(cfg,'yes@example.org') if kind=='calendar' else ws._find_email_account_config(cfg,'yes@example.org')

@pytest.mark.parametrize('kind', ['email','calendar','contacts'])
def test_s3_singletons_and_disabled(ws, kind):
    getter={'email':ws._email_account_configs,'calendar':ws._calendar_accounts,'contacts':ws._contact_account_configs}[kind]
    cfg={kind:{'backend':'google_workspace','address':'yes@example.org','enabled':True}}
    assert getter(cfg)[0]['address']=='yes@example.org'
    cfg[kind]['enabled']=False
    assert getter(cfg)==[]

@pytest.mark.parametrize('backend,enabled,allowed', [('himalaya',None,True),('imap',None,True),('unknown',None,False),('unknown',True,True)])
def test_s3_lookup_admission_positive_controls(ws, monkeypatch, backend, enabled, allowed):
    cfg={'email':{'accounts':[{'backend':backend,'address':'yes@example.org','enabled':enabled}]}}
    assert bool(ws._find_email_account_config(cfg,'yes@example.org')) is allowed
    monkeypatch.setenv('AIWERK_CUI_EMAIL_BACKEND','imap')
    monkeypatch.setenv('AIWERK_CUI_EMAIL_ACCOUNT','yes@example.org')
    assert ws._find_email_account_config({},'other@example.org') is None

@pytest.mark.parametrize('backend', ['microsoft_calendar','microsoft-calendar','microsoft','outlook','outlook_calendar','outlook-calendar'])
def test_s3_microsoft_alias_summary_viewer(ws, monkeypatch, backend):
    cfg={'calendar':{'accounts':[{'backend':backend,'address':'yes@example.org'}]}}
    calls=[]
    monkeypatch.setattr(ws,'_call_aiwerk_bridge_tool',lambda *a,**kw: calls.append(kw['tool']) or {'content':[{'type':'text','text':'{"value":[{"id":"event","subject":"Title"}]}'}]})
    out=ws._calendar_summary(cfg)
    assert calls==['get-calendar-view']
    out['accounts'][0]['items'][0]['source']=backend
    monkeypatch.setattr(ws,'load_config',lambda:cfg)
    monkeypatch.setattr(ws,'_assistant_resources_payload',lambda *a,**kw:{'calendar':out})
    monkeypatch.setattr(ws,'_fetch_microsoft_calendar_event_detail',lambda *a: calls.append('ms-detail') or {})
    monkeypatch.setattr(ws,'_fetch_google_workspace_calendar_event_detail',lambda *a: calls.append('google-detail') or {})
    ws.view_assistant_calendar_event(request(ws),'yes@example.org','event')
    assert calls==['get-calendar-view','ms-detail']
    with pytest.raises(ws.HTTPException): ws.view_assistant_calendar_event(request(ws),'other@example.org','event')
    with pytest.raises(ws.HTTPException): ws.view_assistant_calendar_event(request(ws,authorized=False),'yes@example.org','event')

@pytest.mark.parametrize('key', ['address','email','microsoft_email','outlook_email','user_principal_name'])
def test_s3_microsoft_address_label_roundtrip(ws, monkeypatch, key):
    acct={'backend':'microsoft_calendar','name':' Work ',key:' person@example.org '}
    monkeypatch.setattr(ws,'_call_aiwerk_bridge_tool',lambda *a,**kw:{'content':[{'type':'text','text':'{"value":[{"id":"event"}]}'}]})
    result=ws._microsoft_calendar_summary({},acct)
    assert result['address']=='person@example.org' and result['label']=='Work'
    assert result['items'][0]['account_label']=='Work'
    assert ws._calendar_account_config_for_ref({'calendar':{'accounts':[acct]}},result['address'])==acct
    assert ws._calendar_account_config_for_ref({'calendar':{'accounts':[acct]}},'Work')==acct


@pytest.mark.parametrize('backend', ['aiwerk_bridge','aiwerk-bridge','google_workspace','google-workspace','gmail','mcp','google'])
def test_s3_final_google_alias_real_consumers(ws, monkeypatch, backend):
    cfg={'assistant':{'mailbox':{'accounts':[{'backend':backend,'address':'owner@example.org','user_google_email':'owner@example.org'}]}}}
    calls=[]
    def bridge(*a, **kw):
        calls.append(kw)
        return {'content':[{'type':'text','text':''}]}
    monkeypatch.setattr(ws,'_call_aiwerk_bridge_tool',bridge)
    monkeypatch.setattr(ws,'_maildir_email_summary',lambda *a:None)
    email=ws._email_summary(cfg)
    calendar=ws._calendar_summary(cfg)
    assert email['accounts'][0]['address']=='owner@example.org'
    assert calendar['accounts'][0]['address']=='owner@example.org'
    assert ws._find_email_account_config(cfg,'owner@example.org')['backend']==backend
    assert ws._calendar_account_config_for_ref(cfg,'owner@example.org')['backend']==backend
    assert 'owner@example.org' in ws._contacts_own_email_set(cfg,email,calendar)
    assert ws._contact_account_configs(cfg)[0]['user_google_email']=='owner@example.org'
    assert calls and all(c['params'].get('user_google_email')=='owner@example.org' for c in calls)
    assert ws._find_email_account_config(cfg,'foreign@example.org') is None
    assert ws._calendar_account_config_for_ref(cfg,'foreign@example.org')=={}


@pytest.mark.parametrize('metadata,label,address', [
    ({},'Microsoft Kalender','Microsoft Kalender'),
    ({'name':' Work '},'Work','Work'),
    ({'label':' Preferred ','name':'Ignored'},'Preferred','Preferred'),
    ({'email':' person@example.org '},'person@example.org','person@example.org'),
    ({'user_google_email':' person@example.org '},'person@example.org','person@example.org'),
    ({'address':'me','email':' person@example.org '},'person@example.org','person@example.org'),
])
@pytest.mark.parametrize('outcome',['success','auth','exception'])
def test_s3_final_ms_labels_errors(ws, monkeypatch, metadata,label,address,outcome):
    acct={'backend':'microsoft_calendar',**metadata}
    def bridge(*a,**kw):
        if outcome=='exception': raise RuntimeError('token expired')
        if outcome=='auth': return {'isError':True,'content':[{'type':'text','text':'token expired'}]}
        return {'content':[{'type':'text','text':'{"value":[{"id":"event"}]}'}]}
    monkeypatch.setattr(ws,'_call_aiwerk_bridge_tool',bridge)
    result=ws._calendar_summary({'calendar':{'accounts':[acct]}})['accounts'][0]
    assert result['label']==label and result['address']==address
    assert result['status']=={'success':'connected','auth':'auth_required','exception':'error'}[outcome]
    if outcome=='success': assert result['items'][0]['account_label']==label
    else: assert result['items']==[]
    assert ws._calendar_account_config_for_ref({'calendar':{'accounts':[acct]}},address)==acct


@pytest.mark.parametrize('kind,groups', [('calendar',['accounts','google_workspace','google','microsoft_calendar','microsoft','outlook']),('email',['accounts','google_workspace','gmail','imap','himalaya']),('contacts',['accounts','google_workspace'])])
@pytest.mark.parametrize('shape',['list','dict','nested'])
def test_s3_final_producer_shapes_consumers(ws,kind,groups,shape):
    import copy
    getter={'calendar':ws._calendar_accounts,'email':ws._email_account_configs,'contacts':ws._contact_account_configs}[kind]
    for group in groups:
        item={'address':'owner@example.org','server':'override','backend':'google_workspace'}
        raw=[item,{'address':'disabled@example.org','enabled':False}] if shape=='list' else item if shape=='dict' else {'server':'default','folder':'Inbox','accounts':[item,{'address':'disabled@example.org','enabled':False}]}
        cfg={kind:{group:raw}}; before=copy.deepcopy(cfg)
        out=getter(cfg)
        assert len(out)==1 and out[0]['address']=='owner@example.org' and out[0]['server']=='override'
        if shape=='nested': assert out[0]['folder']=='Inbox'
        assert 'owner@example.org' in ws._contacts_own_email_set(cfg,{}, {})
        assert 'disabled@example.org' not in ws._contacts_own_email_set(cfg,{}, {})
        assert cfg==before


@pytest.mark.parametrize('kind,alias',[('calendar','calendars'),('email','mailbox')])
def test_s3_final_precedence_invalid_and_disabled(ws,monkeypatch,kind,alias):
    getter=ws._calendar_accounts if kind=='calendar' else ws._email_account_configs
    account=lambda address:{'backend':'google_workspace','address':address}
    cfg={'assistant':{alias:{'accounts':[account('assistant@example.org')]}},'dashboard':{kind:{'accounts':[account('dashboard@example.org')]}},kind:{'accounts':[account('root@example.org')]}}
    assert getter(cfg)[0]['address']=='assistant@example.org'
    cfg['assistant']='invalid'
    assert getter(cfg)[0]['address']=='dashboard@example.org'
    cfg['dashboard']=[]
    assert getter(cfg)[0]['address']=='root@example.org'
    monkeypatch.setenv('AIWERK_CUI_EMAIL_BACKEND','google_workspace')
    monkeypatch.setenv('AIWERK_CUI_GOOGLE_EMAIL','foreign@example.org')
    cfg={kind:{'enabled':False,'accounts':[account('disabled@example.org')]}}
    assert getter(cfg)==[]
    calls=[]
    monkeypatch.setattr(ws,'_call_aiwerk_bridge_tool',lambda *a,**k:calls.append(k) or {})
    monkeypatch.setattr(ws,'_maildir_email_summary',lambda *a:None)
    (ws._calendar_summary if kind=='calendar' else ws._email_summary)(cfg)
    assert calls==[]


def test_s3_final_disabled_summary_no_env_fallback(ws,monkeypatch):
    monkeypatch.setenv('AIWERK_CUI_EMAIL_BACKEND','google_workspace')
    monkeypatch.setenv('AIWERK_CUI_GOOGLE_EMAIL','foreign@example.org')
    calls=[]
    monkeypatch.setattr(ws,'_call_aiwerk_bridge_tool',lambda *a,**kw:calls.append(kw) or {})
    monkeypatch.setattr(ws,'_maildir_email_summary',lambda *a:None)
    ws._email_summary({'email':{'enabled':False,'accounts':[]}})
    assert calls==[]


def request(ws, peer='127.0.0.1', headers=None, authorized=True):
    # Exercise the unchanged gated-session admission, not a mocked token guard.
    from types import SimpleNamespace
    app = SimpleNamespace(state=SimpleNamespace(auth_required=True))
    return Request({'type': 'http', 'client': (peer, 1234), 'headers': [(k.encode(), v.encode()) for k,v in (headers or {'host':'localhost'}).items()], 'app': app, 'state': {'session': object()} if authorized else {}})

@pytest.mark.parametrize('peer,headers,allowed', [
    ('203.0.113.9', {'host':'localhost'}, False),
    ('127.0.0.1', {'host':'localhost','x-forwarded-for':'127.0.0.1'}, False),
    ('127.0.0.1', {'host':'localhost','x-real-ip':'127.0.0.1'}, False),
    ('127.0.0.1', {'host':'localhost'}, True),
    ('::1', {'host':'[::1]:8080'}, True),
])
def test_shared_folder_open_requires_real_local_peer_or_optin(ws, monkeypatch, tmp_path, peer, headers, allowed):
    monkeypatch.setattr(ws, '_shared_folder_root', lambda: tmp_path)
    monkeypatch.setattr(ws, '_resolve_shared_folder_root', lambda c: tmp_path)
    monkeypatch.setenv('DISPLAY', ':synthetic')
    monkeypatch.setattr(ws.shutil, 'which', lambda cmd: '/synthetic/'+cmd)
    calls=[]
    monkeypatch.setattr(ws.subprocess, 'Popen', lambda *a, **k: calls.append(a))
    req=request(ws, peer, headers)
    assert ws._shared_folder_summary({}, req)['can_open_folder'] is allowed
    if allowed:
        assert ws.open_assistant_shared_folder_root(req)['ok']
    else:
        with pytest.raises(ws.HTTPException) as exc:
            ws.open_assistant_shared_folder_root(req)
        assert exc.value.status_code == 409
    assert bool(calls) is allowed

@pytest.mark.parametrize('section', ['assistant','dashboard','shared_folder','shared'])
@pytest.mark.parametrize('key', ['allow_remote_file_manager_open','allow_remote_open_folder','remote_file_manager_open'])
def test_shared_remote_open_optin_alias_matrix(ws, monkeypatch, tmp_path, section, key):
    config={section:{key:' On '}}
    monkeypatch.setattr(ws, 'load_config', lambda: config)
    monkeypatch.setattr(ws, '_shared_folder_root', lambda: tmp_path)
    monkeypatch.setattr(ws, '_resolve_shared_folder_root', lambda c: tmp_path)
    monkeypatch.setenv('DISPLAY', ':synthetic')
    monkeypatch.setattr(ws.shutil, 'which', lambda c: '/synthetic/'+c)
    monkeypatch.setattr(ws.subprocess, 'Popen', lambda *a, **k: None)
    req=request(ws,'203.0.113.9', {'host':'remote.example'})
    assert ws._shared_folder_summary(config,req)['can_open_folder']
    assert ws.open_assistant_shared_folder_root(req)['ok']

@pytest.mark.parametrize('failure', ['display','opener','directory','spawn'])
def test_local_open_fails_closed(ws, monkeypatch, tmp_path, failure):
    monkeypatch.setenv('DISPLAY', ':synthetic')
    monkeypatch.delenv('WAYLAND_DISPLAY', raising=False)
    monkeypatch.setattr(ws.shutil,'which',lambda c: '/synthetic/'+c)
    calls=[]
    def spawn(*a, **k):
        calls.append(a)
        if failure == 'spawn':
            raise OSError('synthetic failure')
    monkeypatch.setattr(ws.subprocess,'Popen',spawn)
    path=tmp_path
    if failure == 'display': monkeypatch.delenv('DISPLAY')
    if failure == 'opener': monkeypatch.setattr(ws.shutil,'which',lambda c: None)
    if failure == 'directory': path=tmp_path/'missing'
    assert ws._open_system_folder(path,request=request(ws),config={}) is False
    assert len(calls) == (1 if failure == 'spawn' else 0)

def test_contacts_store_respects_context_home(ws, monkeypatch, tmp_path):
    scoped=tmp_path/'scoped'
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    token = set_hermes_home_override(scoped)
    try:
        ws._write_contacts_store_payload({'contacts':[{'email':'ada@example.org'}]})
        assert (scoped/'cui_contacts.json').exists()
        assert not (tmp_path/'cui_contacts.json').exists()
        assert ws._read_contacts_store_payload()['contacts'][0]['email']=='ada@example.org'
    finally:
        reset_hermes_home_override(token)

@pytest.mark.parametrize('raw, expected', [('Straße','strasse'),('東京','東京'),('=?utf-8?q?J=C3=B6rg?=','jorg'),(' A\x00  B ','a b'),('X'*510,'x'*500)])
def test_contact_search_normalizer_full_unicode_contract(ws, raw, expected):
    assert ws._normalize_contact(raw)==expected
    assert ws._contact_matches_query({'display_name':raw},expected)
    assert 'name:'+expected in ws._contact_hide_keys({'display_name':raw})

def test_contact_filter_preserves_human_phone_and_excludes_system_display(ws):
    rows=[{'display_name':'Ada','phone':'+41 79 123'}, {'display_name':'root','email':'person@example.org'}, {'email':'ertesites@kozpontirendszer.gov.hu'}, {'email':'me@example.org'}, {'email':'synthetic@example.org'}, {'display_name':'Grace','email':'grace@example.org'}]
    out=ws._filter_contacts_payload({'items':rows},own_emails={'me@example.org'})
    assert [c['display_name'] for c in out['items']]==['Ada','Grace']

@pytest.mark.parametrize('raw', [{'email':'Ada@Example.org','display_name':'Ada: Test!','key':'PERSIST'}, {'phone':'+41 79','display_name':'Phone Only'}, {'email':'long@example.org','display_name':'X'*120}])
def test_contact_generated_id_compatibility(ws, raw):
    import re
    c=ws._normalize_contact_item(raw)
    expected=re.sub(r'[^A-Za-z0-9._:-]+','-', '|'.join([c['email'],c['phone'],c['display_name']])).strip('.-_:')[:120] or 'contact'
    assert c.get('id')==expected
    assert c.get('key')==raw.get('key')
    assert ws._normalize_contact_item(c)['id']==expected[:80]
    assert ws._normalize_contact_item({**raw,'id':' A\x00B '+'Z'*100})['id']==('A B '+'Z'*100)[:80]

def test_contact_hide_ui_payload_roundtrip(ws, monkeypatch, tmp_path):
    c=ws._normalize_contact_item({'email':'ada@example.org','display_name':'Ada'})
    body={'id':c.get('id','ada-example.org-Ada'),'email':c['email'],'phone':c['phone'],'display_name':c['display_name']}
    result=ws.hide_cui_contact(request(ws),ws.CuiContactHideRequest(**body))
    assert result.get('hidden')
    assert '' not in result['hidden']
    assert ws._filter_hidden_contacts([c])==[]
    assert ws._filter_contacts_payload({'items':[c]},own_emails=set())['items']==[]
    with pytest.raises(ws.HTTPException) as exc:
        ws.hide_cui_contact(request(ws),ws.CuiContactHideRequest())
    assert exc.value.status_code==400
    with pytest.raises(ws.HTTPException) as exc:
        ws.hide_cui_contact(request(ws,authorized=False),ws.CuiContactHideRequest(**body))
    assert exc.value.status_code==401
    phone={'display_name':'Phone','phone':'+41 79 123'}
    ws.hide_cui_contact(request(ws),ws.CuiContactHideRequest(phone=phone['phone']))
    assert ws._filter_hidden_contacts([phone])==[]

def test_address_contacts_retain_interaction_metadata(ws):
    # Existing producer call shape must gain default metadata, not TypeError RED.
    c=ws._contacts_from_address_text('jane_doe@example.org, bad <invalid>',source='Gmail')[0]
    assert c['display_name']=='Jane Doe'
    assert c.get('interaction_count')==1
    assert c.get('interaction_score')==1.0
    assert c.get('relevance')=='frequent'
    assert c['source_badges']==['Gmail','Häufig']

def test_contact_summary_and_search_hide_same_identity(ws, monkeypatch):
    raw={'display_name':'Ada','phone':'+41 79 123'}
    monkeypatch.setattr(ws,'_read_manual_contacts',lambda:[raw])
    monkeypatch.setattr(ws,'_contacts_from_google_workspace',lambda *a,**k:[])
    monkeypatch.setattr(ws,'_contacts_from_google_workspace_interactions',lambda *a,**k:[])
    monkeypatch.setattr(ws,'_contacts_from_himalaya_interactions',lambda *a,**k:[])
    monkeypatch.setattr(ws,'_assistant_resources_payload',lambda **k:{'email':{},'calendar':{},'contacts':{'items':[raw]}})
    # S8 row44/operator FINISH authority: this sweep test owns visibility,
    # not classification of unannotated manual contacts as relevant.
    before=ws._contacts_summary({}, {}, {})
    assert len(before['relevant']+before['frequent'])==1
    assert len(ws._search_contacts_payload()['items'])==1
    ws.hide_cui_contact(request(ws),ws.CuiContactHideRequest(phone=raw['phone']))
    after=ws._contacts_summary({}, {}, {})
    assert after['relevant']+after['frequent']==[]
    assert ws._search_contacts_payload()['items']==[]

def test_contact_metadata_is_normalized_before_ranking(ws):
    raw={'email':'ada@example.org','interaction_count':'2','interaction_score':'3.456','last_interaction_at':' now\x00 ', 'organization':' Company\x00 ', 'role':' Engineer\n ', 'relevance':' relevant\x00 '}
    c=ws._normalize_contact_item(raw)
    assert c['interaction_count']==2
    assert c['interaction_score']==3.46
    assert c['last_interaction_at']=='now'
    assert c['organization']=='Company'
    assert c['role']=='Engineer'
    assert c['relevance']=='relevant'
    bad=ws._normalize_contact_item({'interaction_count':'bad','interaction_score':'bad'})
    assert not bad.get('interaction_count')
    assert not bad.get('interaction_score')


@pytest.mark.parametrize('backend', ['google_workspace', 'himalaya'])
def test_email_rows_selection_producer(ws, monkeypatch, backend):
    import copy
    unread=[{'id':' 42 ', 'message_id':' 42 ', 'date':'2026-09-01T00:00:00Z', 'received_at':'2026-09-01T00:00:00Z'},
            {'id':'43','date':'2026-09-03T00:00:00Z','received_at':'2026-09-03T00:00:00Z'},
            {'id':'spam','from':{'name':'Migros','addr':'fraud@example.org'},'subject':'Migros alert'}]
    latest=[{'id':'42'},{'id':'44','received_at':'2099-01-01T00:00:00Z'}, {'id':'45'}]
    original=copy.deepcopy((unread,latest))
    monkeypatch.setattr(ws,'_ASSISTANT_EMAIL_PREVIEW_ITEMS',4)
    calls=[]
    if backend=='himalaya':
        monkeypatch.setattr(ws,'_run_himalaya_envelope_list',lambda **kw:calls.append(kw) or (unread if kw.get('query') else latest))
        out=ws._himalaya_email_summary({}, {'backend':backend,'account':'chosen','folder':'Inbox'})
        assert calls[0]['query']=='not flag Seen' and calls[0]['account']=='chosen'
    else:
        monkeypatch.setattr(ws,'_gmail_bridge_search_message_ids',lambda *a,**kw:calls.append((a,kw)) or ['42','43','spam'])
        monkeypatch.setattr(ws,'_gmail_bridge_metadata_items_for_ids',lambda *a,**kw:unread)
        monkeypatch.setattr(ws,'_gmail_bridge_message_items',lambda *a,**kw:calls.append((a,kw)) or latest)
        out=ws._google_workspace_email_summary({}, {'backend':backend,'address':'owner@example.org'})
        assert calls[0][0][1]=='in:inbox is:unread' and calls[1][0][1]=='in:inbox'
    assert [str(i.get('message_id') or i['id']).strip() for i in out['items']]==['43','42','44','45']
    assert [i['unread'] for i in out['items']]==[True,True,False,False]
    assert out['unread_count']==2 and out['filtered_count']==1
    assert all(i.get('open_url') for i in out['items'])
    assert (unread,latest)==original


def test_email_rows_chronology_and_unknown_dates(ws, monkeypatch):
    import copy
    unread=[{'id':'old','received_at':'2026-01-01'}, {'id':'new','received_at':'2026-02-01'}, {'id':'tie','received_at':'2026-02-01'}, {'id':'unknown','received_at':12}]
    original=copy.deepcopy(unread)
    selected=ws._unread_first_email_items(unread,[{'id':'read','received_at':'2099'}],min_items=2)
    assert [i['id'] for i in selected]==['new','tie','old','unknown']
    assert unread==original
    monkeypatch.setattr(ws,'_ASSISTANT_EMAIL_PREVIEW_ITEMS',20)
    summaries=[{'address':'a@example.org','unread_count':1,'items':[{'id':'a','unread':True,'received_at':'2026-01-01'},{'id':'r1','unread':False,'received_at':'2098'}]},
               {'address':'b@example.org','unread_count':1,'items':[{'id':'b','unread':True,'received_at':'2026-02-01'},{'id':'r2','unread':False,'received_at':'2099'}]}]
    old=copy.deepcopy(summaries)
    out=ws._merge_email_summaries(summaries)
    assert [i['id'] for i in out['items']]==['b','a','r2','r1']
    assert out['unread_count']==2 and summaries==old
    blanks=ws._unread_first_email_items([{'message_id':'   '},{'id':''}], [{'id':'read'}], min_items=3)
    assert len(blanks)==3
    latest=ws._unread_first_email_items([], [{'id':'older','received_at':'2000'},{'id':'newer','received_at':'2099'}], min_items=2)
    assert [i['id'] for i in latest]==['older','newer']


def test_email_rows_himalaya_real_envelope_projection(ws, monkeypatch):
    import copy,json,subprocess
    from urllib.parse import urlsplit,parse_qs
    raw={'id':' /42? ', 'subject':'  Hello  ', 'date':'2026-09-01 12:30+02:00', 'from':{'name':'Ada','addr':'ada@example.org'},'flags':['Seen'],'has_attachment':True}
    original=copy.deepcopy(raw); calls=[]
    monkeypatch.setattr(ws.shutil,'which',lambda *a:'/fake/himalaya')
    def run(cmd,**kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd,0,json.dumps([raw]),'')
    monkeypatch.setattr(ws.subprocess,'run',run)
    out=ws._himalaya_email_summary({}, {'backend':'himalaya','account':'chosen','folder':'Inbox'})
    item=out['items'][0]
    assert item.get('message_id')=='/42?'
    assert item['id']==ws._safe_resource_id('/42?')
    assert item['subject']=='Hello' and item['received_at']=='2026-09-01T10:30:00Z'
    assert item['sender']=='Ada <ada@example.org>' and item['from']==raw['from']
    assert item['has_attachment'] is True and item['unread'] is True
    assert parse_qs(urlsplit(item['open_url']).query)['id']==['/42?']
    assert calls[0][calls[0].index('--account')+1]=='chosen' and raw==original
