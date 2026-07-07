import json
import os
from types import SimpleNamespace


def test_cui_actor_model_config_is_stamped_for_sessions(monkeypatch):
    from run_agent import AIAgent

    monkeypatch.setenv(
        "AIWERK_CUI_ACTOR_CONTEXT",
        json.dumps({"tenant_id": "example-tenant", "actor_id": "aiwerk:operator:admin", "role": "admin"}),
    )
    agent = AIAgent(provider="openai", model="gpt-4.1", api_key="test-key", base_url="http://127.0.0.1:9/v1", skip_memory=True)

    cfg = agent._session_init_model_config
    assert cfg["_cui_visibility_scope"] == "admin"
    assert cfg["_cui_actor_role"] == "admin"
    assert cfg["_cui_actor_id"] == "aiwerk:operator:admin"
    assert cfg["_cui_tenant_id"] == "example-tenant"


def test_customer_cui_actor_cannot_see_tagged_admin_sessions():
    from hermes_cli import web_server

    admin_session = {
        "id": "admin-session",
        "model_config": json.dumps(
            {
                "_cui_visibility_scope": "admin",
                "_cui_actor_role": "admin",
                "_cui_actor_id": "aiwerk:attila:admin",
                "_cui_tenant_id": "example-tenant",
            }
        ),
    }
    user_actor = {"tenant_id": "example-tenant", "actor_id": "example-tenant:customer:user", "role": "user"}
    other_admin_actor = {"tenant_id": "example-tenant", "actor_id": "aiwerk:operator:admin", "role": "admin"}
    same_admin_actor = {"tenant_id": "example-tenant", "actor_id": "aiwerk:attila:admin", "role": "admin"}

    assert web_server._session_visible_to_cui_actor(admin_session, user_actor) is False
    assert web_server._session_visible_to_cui_actor(admin_session, other_admin_actor) is False
    assert web_server._session_visible_to_cui_actor(admin_session, same_admin_actor) is True


def test_customer_cui_actor_cannot_see_legacy_untagged_internal_sessions():
    from hermes_cli import web_server

    user_actor = {"tenant_id": "example-tenant", "actor_id": "example-tenant:customer:user", "role": "user"}
    admin_actor = {"tenant_id": "example-tenant", "actor_id": "aiwerk:operator:admin", "role": "admin"}

    for source in ["cli", "tui", "cron", "classifier", "reflection", "system", "internal"]:
        session = {"id": f"legacy-{source}", "source": source, "model_config": None}
        assert web_server._session_visible_to_cui_actor(session, user_actor) is False
        assert web_server._session_visible_to_cui_actor(session, admin_actor) is True

    legacy_human_session = {"id": "legacy-web", "source": "web", "model_config": None}
    assert web_server._session_visible_to_cui_actor(legacy_human_session, user_actor) is False


def test_customer_cui_actor_only_sees_own_tagged_customer_sessions():
    from hermes_cli import web_server

    susanne_session = {
        "id": "susanne-session",
        "model_config": json.dumps(
            {
                "_cui_visibility_scope": "customer",
                "_cui_actor_role": "user",
                "_cui_actor_id": "example-tenant:customer:user",
                "_cui_tenant_id": "example-tenant",
            }
        ),
    }
    other_customer_session = {
        "id": "other-session",
        "model_config": json.dumps(
            {
                "_cui_visibility_scope": "customer",
                "_cui_actor_role": "user",
                "_cui_actor_id": "example-tenant:other:user",
                "_cui_tenant_id": "example-tenant",
            }
        ),
    }
    user_actor = {"tenant_id": "example-tenant", "actor_id": "example-tenant:customer:user", "role": "user"}

    assert web_server._session_visible_to_cui_actor(susanne_session, user_actor) is True
    assert web_server._session_visible_to_cui_actor(other_customer_session, user_actor) is False


def test_cui_actor_context_from_authenticated_request(monkeypatch):
    from hermes_cli import web_server

    monkeypatch.setattr(web_server, "_assistant_mode_enabled", lambda: True)
    request = SimpleNamespace(
        state=SimpleNamespace(
            session=SimpleNamespace(
                tenant_id="example-tenant",
                org_id="",
                actor_id="example-tenant:customer:user",
                user_id="Customer",
                role="user",
            )
        )
    )

    assert web_server._cui_actor_context_from_request(request) == {
        "tenant_id": "example-tenant",
        "actor_id": "example-tenant:customer:user",
        "role": "user",
    }


def test_cui_actor_context_fails_closed_when_metadata_empty(monkeypatch):
    # A logged-in customer in assistant mode whose auth provider does NOT
    # populate tenant/actor/role must NOT yield {} (which the visibility filter
    # reads as the trusted admin/loopback path). It returns the restricted
    # sentinel so the customer view fails closed.
    from hermes_cli import web_server

    monkeypatch.setattr(web_server, "_assistant_mode_enabled", lambda: True)
    request = SimpleNamespace(
        state=SimpleNamespace(
            session=SimpleNamespace(
                tenant_id="", org_id="", actor_id="", user_id="", role="",
            )
        )
    )
    actor = web_server._cui_actor_context_from_request(request)
    assert actor.get("_restricted") == "1"


def test_restricted_actor_sees_no_sessions(monkeypatch):
    # The restricted sentinel must make every session invisible (fail closed),
    # including customer-tagged ones with no proven ownership.
    from hermes_cli import web_server

    restricted = {"role": "user", "_restricted": "1"}
    tagged_customer = {
        "id": "cust-session",
        "model_config": json.dumps(
            {
                "_cui_visibility_scope": "customer",
                "_cui_actor_role": "user",
                "_cui_actor_id": "example-tenant:customer:user",
                "_cui_tenant_id": "example-tenant",
            }
        ),
    }
    untagged = {"id": "x", "source": "tui", "model_config": None}
    assert web_server._session_visible_to_cui_actor(tagged_customer, restricted) is False
    assert web_server._session_visible_to_cui_actor(untagged, restricted) is False
    # And the unconfined path (no actor at all) is unchanged.
    assert web_server._session_visible_to_cui_actor(untagged, {}) is True


def test_customer_cui_actor_only_sees_linked_telegram_sessions(monkeypatch):
    from hermes_cli import web_server

    cfg = {
        "dashboard": {
            "basic_auth": {
                "users": [
                    {
                        "actor_id": "example-tenant:customer:user",
                        "user_id": "Customer",
                        "tenant_id": "example-tenant",
                        "role": "user",
                        "telegram_user_ids": ["1461953838"],
                    }
                ]
            }
        }
    }
    monkeypatch.setattr(web_server, "load_config", lambda: cfg)
    user_actor = {
        "tenant_id": "example-tenant",
        "actor_id": "example-tenant:customer:user",
        "role": "user",
        "user_id": "Customer",
    }

    own_telegram = {"id": "own-tg", "source": "telegram", "user_id": "1461953838", "model_config": None}
    other_telegram = {"id": "other-tg", "source": "telegram", "user_id": "1392690488", "model_config": None}

    assert web_server._session_visible_to_cui_actor(own_telegram, user_actor) is True
    assert web_server._session_visible_to_cui_actor(other_telegram, user_actor) is False
