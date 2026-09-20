"""Installed RPC admission, before resource scopes and handler side effects."""
from contextvars import ContextVar
from functools import wraps
from pathlib import Path

from agent.cui_actor_context import current_bound_cui_actor_context
from hermes_cli.dashboard_auth.profile_access import (
    ProfileAccessDenied, _snapshot, authorize, authorized_profiles, valid_profile,
)

_rpc_scope: ContextVar[tuple[str, tuple[str, ...]] | None] = ContextVar("rpc_profile_scope", default=None)

_ACTIONS = {}
for _action, _names in {
    'session.list': 'session.list session.most_recent session.active_list',
    'session.read': ('session.history session.status session.usage session.context_breakdown '
                     'session.control.read session.events.since session.events.stats'),
    'session.mutate': ('session.title session.delete session.set_hidden session.close '
                       'session.interrupt session.cwd.set session.workspace.move session.control'),
    'profile.admin': ('config.get config.set setup.status setup.runtime_check model.options '
                      'profiles.describe profiles.configure profiles.create profiles.get_asset profiles.set_asset '
                      'mcp.catalog mcp.servers.list mcp.servers.status mcp.servers.add mcp.servers.remove '
                      'mcp.servers.set_api_key mcp.servers.test mcp.servers.oauth.start mcp.servers.oauth.poll '
                      'mcp.servers.oauth.cancel mcp.servers.oauth.callback tools.configure reload.mcp'),
    'profile.use': 'prompt.submit prompt.learn prompt.background prompt.btw',
    'profile.discover': 'profiles.list',
}.items():
    for _name in _names.split():
        _ACTIONS[_name] = (_action,)
for _name in ('session.create', 'session.undo', 'session.compress', 'session.branch',
              'session.side.start', 'session.side.back'):
    _ACTIONS[_name] = ('profile.use', 'session.mutate')
for _name in ('session.resume', 'session.activate'):
    _ACTIONS[_name] = ('session.resume', 'profile.use')
_ACTIONS['session.save'] = ('session.export', 'profile.use', 'session.mutate')


def live_profile(record):
    """Stored context, not a client selector or a filesystem probe."""
    profile = record.get('profile')
    home = record.get('profile_home')
    if home:
        path = Path(home)
        derived = path.name if path.parent.name == 'profiles' else 'default'
        if profile is not None and profile != derived:
            raise ProfileAccessDenied('profile access denied')
        profile = derived
    if not valid_profile(profile):
        raise ProfileAccessDenied('profile access denied')
    return profile


def authorize_session(record, *actions):
    """Recheck saved server authority before any deferred resource work."""
    actor = record.get("cui_actor_context")
    snapshot, _ = _snapshot(actor)
    if snapshot is None:
        return
    profile = live_profile(record)
    for action in actions or ("profile.use",):
        authorize(actor, action, profile)
    if record.get("profile_home"):
        from hermes_cli.profiles import get_profile_dir
        if Path(record["profile_home"]) != get_profile_dir(profile):
            raise ProfileAccessDenied("profile access denied")


def compute_actor_scope(frame, cached=None, actions=("profile.use",)):
    from contextlib import contextmanager
    from agent.cui_actor_context import bind_cui_actor_context, reset_cui_actor_context

    @contextmanager
    def scope():
        actor = frame.get("cui_actor_context")
        snapshot, _ = _snapshot(actor)
        if snapshot is not None:
            if cached is not None:
                if actor != cached.get("cui_actor_context"):
                    raise ProfileAccessDenied("profile access denied")
                for key in ("profile", "profile_home"):
                    if key in frame and frame[key] != cached.get(key):
                        raise ProfileAccessDenied("profile access denied")
            authorize_session(cached if cached is not None else frame, *actions)
        token = bind_cui_actor_context(actor)
        try:
            yield
        finally:
            reset_cui_actor_context(token)
    return scope()


def _selectors(params):
    for key in ('profile', 'name', 'clone_from', 'session_id', 'session_key', 'parent_session_id'):
        if key not in params:
            continue
        value = params[key]
        if (type(value) is not str or not value.strip() or value != value.strip()
                or '/' in value or '\\' in value or any(ord(c) < 32 for c in value)
                or value in {'.', '..', '*'}):
            raise ProfileAccessDenied('profile access denied')
        if key in {'profile', 'clone_from'} and not valid_profile(value):
            raise ProfileAccessDenied('profile access denied')


def authorize_lineage(db, session_id, profile, *, allow_missing=False):
    """Preflight exactly the compression rows changed by set_session_hidden.

    Read raw rows: an ownership-filtering DB view must not hide a foreign
    member and turn a partial lineage into a successful authorization.
    """
    import json
    from hermes_cli.dashboard_auth.session_ownership import has_exact_cui_session_owner
    actor = current_bound_cui_actor_context()
    snapshot, _ = _snapshot(actor)
    if snapshot is None:
        return
    authorize(actor, 'session.mutate', profile)
    with db._read_ctx() as conn:
        ids = conn.execute("""
            WITH RECURSIVE ancestors(id) AS (
                SELECT ? UNION SELECT parent.id FROM ancestors a
                JOIN sessions child ON child.id=a.id
                JOIN sessions parent ON parent.id=child.parent_session_id
                WHERE parent.end_reason='compression'
                  AND COALESCE(json_extract(COALESCE(child.model_config, '{}'), '$._side_from'), '') != parent.id
                  AND NOT EXISTS (SELECT 1 FROM session_stack s WHERE s.side_session_id=child.id)
            ), descendants(id) AS (
                SELECT ? UNION SELECT child.id FROM descendants d
                JOIN sessions parent ON parent.id=d.id
                JOIN sessions child ON child.parent_session_id=parent.id
                WHERE parent.end_reason='compression'
                  AND COALESCE(json_extract(COALESCE(child.model_config, '{}'), '$._side_from'), '') != parent.id
                  AND NOT EXISTS (SELECT 1 FROM session_stack s WHERE s.side_session_id=child.id)
            ) SELECT id FROM ancestors UNION SELECT id FROM descendants
        """, (session_id, session_id)).fetchall()
        for (key,) in ids:
            row = conn.execute('SELECT profile_name, model_config FROM sessions WHERE id=?', (key,)).fetchone()
            if row is None and allow_missing and key == session_id:
                continue
            if row is None or row['profile_name'] != profile:
                raise ProfileAccessDenied('profile access denied')
            try:
                config = json.loads(row['model_config'] or '{}')
            except (TypeError, ValueError):
                raise ProfileAccessDenied('profile access denied') from None
            if not isinstance(config, dict) or not has_exact_cui_session_owner(config, actor):
                raise ProfileAccessDenied('profile access denied')


def _roster(server, rid, params, actor):
    """Root-granted names first; no enumeration of ungranted profile metadata."""
    from hermes_cli.profiles import get_profile_dir, _read_config_model, read_profile_meta, _count_skills
    from utils import is_truthy_value
    rows = []
    for name in authorized_profiles(actor, 'profile.discover') or ():
        row = {'name': name, 'is_default': name == 'default'}
        path = get_profile_dir(name)
        row['path'] = str(path)
        row['model'], row['provider'] = _read_config_model(path)
        meta = read_profile_meta(path)
        row.update(description=meta.get('description') or '', display_name=meta.get('display_name') or '',
                   skill_count=_count_skills(path) or 0)
        server._profile_ui_meta_fields(row, path)
        if is_truthy_value(params.get('include_sessions', True)):
            row.update(last_session=None, worker_session=None, canonical_session=None)
            try:
                authorize(actor, 'session.list', name)
            except ProfileAccessDenied:
                pass
            else:
                # Read-only ownership-filtered previews; never resurrect canonical chats.
                from hermes_state import SessionDB
                db_path = path / 'state.db'
                if db_path.is_file():
                    with SessionDB(db_path=db_path, read_only=True) as raw:
                        db = server._CuiActorScopedSessionDB(raw, actor)
                        row['last_session'], row['worker_session'] = server._latest_profile_session_rows(db)
                        canonical = db.get_session_by_title('Bot Chat')
                        if (canonical and not canonical.get('archived')
                                and not server._denied_source(canonical)
                                and server._session_list_row_visible_to_cui_actor(raw, canonical, actor)):
                            tip = raw.get_compression_tip(canonical['id']) or canonical['id']
                            current = db.get_session(tip)
                            if current and not current.get('archived'):
                                row['canonical_session'] = {
                                    'id': canonical['id'], 'resolved_id': tip, 'root_title': canonical['title'],
                                    'title': current.get('title') or '',
                                    'preview': server._latest_message_preview(db, tip),
                                    'started_at': current.get('started_at') or 0,
                                    'last_active': current.get('last_activity_at') or current.get('started_at') or 0,
                                    'message_count': current.get('message_count') or 0,
                                }
        rows.append(row)
    return server._ok(rid, {'profiles': rows, 'bot_mode_protocol': True})


def installed_handler(server, name, handler):
    @wraps(handler)
    def guarded(rid, params):
        # This transport reply resolves one already-authorized, exact pending
        # request. The initiating worker owns the actor/profile authorization.
        if name == "approval.respond":
            return handler(rid, params)
        actor = current_bound_cui_actor_context()
        try:
            snapshot, authority = _snapshot(actor)
            if snapshot is None:
                if name == 'session.events.since' and actor and not server._live_session_visible_to_cui_actor(
                        params.get('session_id', ''), actor):
                    return server._err(rid, 4001, 'session not found')
                return handler(rid, params)
            if authority is None:
                raise ProfileAccessDenied('profile access denied')
            if not isinstance(params, dict):
                return server._err(rid, -32602, 'invalid params: expected an object')
            _selectors(params)
            actions = _ACTIONS.get(name)
            if actions is None:
                raise ProfileAccessDenied('profile access denied')
            target = params.get('profile', authority.default_profile)
            if name.startswith('profiles.') and name != 'profiles.list':
                target = params.get('name', target)
                if 'profile' in params and target != params['profile']:
                    raise ProfileAccessDenied('profile access denied')
            sid = params.get('session_id')
            if sid and 'profile' not in params:
                # Root-authorized candidate names only; no sensitive live/session
                # lookup when this actor has no profile granting the operation.
                candidates = set(authorized_profiles(actor, actions[0]) or ())
                for action in actions[1:]:
                    candidates.intersection_update(authorized_profiles(actor, action) or ())
                if not candidates:
                    raise ProfileAccessDenied('profile access denied')
            else:
                for action in actions:
                    authorize(actor, action, target)
            if 'clone_from' in params:
                authorize(actor, 'profile.admin', params['clone_from'])
            sid = params.get('session_id')
            record = None
            if sid:
                with server._sessions_lock:
                    record = server._sessions.get(sid)
                if record is not None:
                    effective = live_profile(record)
                    if 'profile' in params and effective != target:
                        raise ProfileAccessDenied('profile access denied')
                    for action in actions:
                        authorize(actor, action, effective)
                    if not server._live_session_visible_to_cui_actor(sid, actor):
                        raise ProfileAccessDenied('profile access denied')
                    target = effective
            if record is None:
                for action in actions:
                    authorize(actor, action, target)
            if name == 'session.events.since' and record is None:
                raise ProfileAccessDenied('profile access denied')
            if name.startswith('prompt.') and any(key in params for key in (
                    'truncate_before_row_id', 'truncate_before_user_ordinal', 'confirm_truncate')):
                authorize(actor, 'session.mutate', target)
            normalized = {**params, 'profile': target}
            if name == 'profiles.list':
                return _roster(server, rid, normalized, actor)
            token = _rpc_scope.set((target, actions))
            try:
                return handler(rid, normalized)
            finally:
                _rpc_scope.reset(token)
        except ProfileAccessDenied:
            return server._err(rid, 403, 'profile access denied')
    return guarded


def live_record_in_scope(record):
    scope = _rpc_scope.get()
    if scope is None:
        return True
    target, actions = scope
    try:
        if live_profile(record) != target:
            return False
        actor = current_bound_cui_actor_context()
        for action in actions:
            authorize(actor, action, target)
        return True
    except ProfileAccessDenied:
        return False
