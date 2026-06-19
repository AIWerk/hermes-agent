import json
import os
from types import SimpleNamespace


def test_cui_actor_model_config_is_stamped_for_sessions(monkeypatch):
    from run_agent import AIAgent

    monkeypatch.setenv(
        "AIWERK_CUI_ACTOR_CONTEXT",
        json.dumps({"tenant_id": "meerwohnen", "actor_id": "aiwerk:attila:admin", "role": "admin"}),
    )
    agent = AIAgent(provider="openai", model="gpt-4.1", api_key="test-key", base_url="http://127.0.0.1:9/v1", skip_memory=True)

    cfg = agent._session_init_model_config
    assert cfg["_cui_visibility_scope"] == "admin"
    assert cfg["_cui_actor_role"] == "admin"
    assert cfg["_cui_actor_id"] == "aiwerk:attila:admin"
    assert cfg["_cui_tenant_id"] == "meerwohnen"


def test_customer_cui_actor_cannot_see_tagged_admin_sessions():
    from hermes_cli import web_server

    admin_session = {
        "id": "admin-session",
        "model_config": json.dumps(
            {
                "_cui_visibility_scope": "admin",
                "_cui_actor_role": "admin",
                "_cui_actor_id": "aiwerk:attila:admin",
                "_cui_tenant_id": "meerwohnen",
            }
        ),
    }
    user_actor = {"tenant_id": "meerwohnen", "actor_id": "meerwohnen:susanne:user", "role": "user"}
    admin_actor = {"tenant_id": "meerwohnen", "actor_id": "aiwerk:attila:admin", "role": "admin"}

    assert web_server._session_visible_to_cui_actor(admin_session, user_actor) is False
    assert web_server._session_visible_to_cui_actor(admin_session, admin_actor) is True


def test_customer_cui_actor_only_sees_own_tagged_customer_sessions():
    from hermes_cli import web_server

    susanne_session = {
        "id": "susanne-session",
        "model_config": json.dumps(
            {
                "_cui_visibility_scope": "customer",
                "_cui_actor_role": "user",
                "_cui_actor_id": "meerwohnen:susanne:user",
                "_cui_tenant_id": "meerwohnen",
            }
        ),
    }
    other_customer_session = {
        "id": "other-session",
        "model_config": json.dumps(
            {
                "_cui_visibility_scope": "customer",
                "_cui_actor_role": "user",
                "_cui_actor_id": "meerwohnen:other:user",
                "_cui_tenant_id": "meerwohnen",
            }
        ),
    }
    user_actor = {"tenant_id": "meerwohnen", "actor_id": "meerwohnen:susanne:user", "role": "user"}

    assert web_server._session_visible_to_cui_actor(susanne_session, user_actor) is True
    assert web_server._session_visible_to_cui_actor(other_customer_session, user_actor) is False


def test_cui_actor_context_from_authenticated_request(monkeypatch):
    from hermes_cli import web_server

    monkeypatch.setattr(web_server, "_assistant_mode_enabled", lambda: True)
    request = SimpleNamespace(
        state=SimpleNamespace(
            session=SimpleNamespace(
                tenant_id="meerwohnen",
                org_id="",
                actor_id="meerwohnen:susanne:user",
                user_id="Susanne",
                role="user",
            )
        )
    )

    assert web_server._cui_actor_context_from_request(request) == {
        "tenant_id": "meerwohnen",
        "actor_id": "meerwohnen:susanne:user",
        "role": "user",
    }
