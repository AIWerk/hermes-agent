import json
from types import SimpleNamespace

import pytest


ACTOR_A = {"tenant_id": "tenant-a", "actor_id": "actor-a", "role": "user"}
ACTOR_B = {"tenant_id": "tenant-a", "actor_id": "actor-b", "role": "user"}


def _row(session_id, actor=None, *, source="tui", visibility_scope="customer"):
    config = None
    if actor is not None:
        config = json.dumps(
            {
                "_cui_visibility_scope": visibility_scope,
                "_cui_actor_role": actor["role"],
                "_cui_actor_id": actor["actor_id"],
                "_cui_tenant_id": actor["tenant_id"],
            }
        )
    return {"id": session_id, "source": source, "model_config": config}


def test_agent_owner_stamp_uses_bound_authenticated_context_not_environment(monkeypatch):
    from agent import agent_init
    from agent.cui_actor_context import bind_cui_actor_context, reset_cui_actor_context

    monkeypatch.setenv(
        "AIWERK_CUI_ACTOR_CONTEXT",
        json.dumps({"tenant_id": "evil", "actor_id": "evil", "role": "admin"}),
    )
    token = bind_cui_actor_context(ACTOR_A)
    try:
        config = {}
        agent_init._stamp_authenticated_cui_session_owner(config)
    finally:
        reset_cui_actor_context(token)

    assert config["_cui_actor_context"] == ACTOR_A
    assert config["_cui_actor_id"] == "actor-a"
    assert config["_cui_tenant_id"] == "tenant-a"
    assert config["_cui_visibility_scope"] == "customer"


def test_confined_visibility_requires_exact_owner_and_hides_all_untagged_rows():
    from hermes_cli import web_server

    assert web_server._session_visible_to_cui_actor(_row("own", ACTOR_A), ACTOR_A)
    for hidden in (
        _row("foreign", ACTOR_B),
        _row("untagged"),
        _row("legacy", source="cli"),
        _row("admin", {"tenant_id": "tenant-a", "actor_id": "admin", "role": "admin"}),
        _row(
            "same-id-admin",
            {**ACTOR_A, "role": "admin"},
            visibility_scope="admin",
        ),
    ):
        assert not web_server._session_visible_to_cui_actor(hidden, ACTOR_A)
    assert web_server._session_visible_to_cui_actor(_row("legacy"), {})


def test_actor_scoped_db_filters_before_count_and_pagination_and_blocks_side_effects():
    from hermes_cli import web_server

    rows = [_row("foreign", ACTOR_B), _row("own-1", ACTOR_A), _row("own-2", ACTOR_A)]

    class DB:
        def __init__(self):
            self.renamed = []

        def list_sessions_rich(self, *, limit, offset=0, **_kwargs):
            return rows[offset : offset + limit]

        def get_session(self, sid):
            return next((row for row in rows if row["id"] == sid), None)

        def get_session_by_title(self, title):
            return self.get_session("foreign" if title == "guessed" else title)

        def rename_session(self, sid, title):
            self.renamed.append((sid, title))
            return True

    raw = DB()
    db = web_server._CuiActorScopedSessionDB(raw, ACTOR_A)
    assert [row["id"] for row in db.list_sessions_rich(limit=1, offset=0)] == ["own-1"]
    assert db.session_count() == 2
    assert db.get_session("foreign") is None
    assert db.get_session_by_title("guessed") is None
    with pytest.raises(web_server._CuiSessionNotFound):
        db.rename_session("foreign", "stolen")
    assert raw.renamed == []


def test_authenticated_request_context_fails_closed_when_identity_incomplete():
    from hermes_cli import web_server

    complete = SimpleNamespace(
        state=SimpleNamespace(
            session=SimpleNamespace(tenant_id="tenant-a", actor_id="actor-a", role="user")
        )
    )
    incomplete = SimpleNamespace(
        state=SimpleNamespace(session=SimpleNamespace(tenant_id="", actor_id="", role="user"))
    )
    assert web_server._cui_actor_context_from_request(complete) == ACTOR_A
    assert web_server._cui_actor_context_from_request(incomplete)["_restricted"] == "1"
