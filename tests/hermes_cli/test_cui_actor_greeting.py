from types import SimpleNamespace

from hermes_cli import web_server


def _request(**session_fields):
    return SimpleNamespace(state=SimpleNamespace(session=SimpleNamespace(**session_fields)))


def test_customer_greeting_uses_authenticated_actor_display_name():
    identity = web_server._assistant_greeting_identity(
        _request(
            tenant_id="tenant-example",
            actor_id="tenant-example:customer:user",
            user_id="customer",
            role="user",
            display_name="Example Customer",
        ),
    )

    assert identity == {"name": "Example", "context": "customer"}


def test_customer_without_authenticated_display_name_does_not_use_config_fallback():
    identity = web_server._assistant_greeting_identity(
        _request(
            tenant_id="tenant-example",
            actor_id="tenant-example:customer:user",
            user_id="customer",
            role="user",
            display_name="",
        ),
    )

    assert identity == {"name": None, "context": "customer"}


def test_admin_greeting_labels_support_context_and_never_uses_customer_name():
    identity = web_server._assistant_greeting_identity(
        _request(
            tenant_id="tenant-example",
            actor_id="aiwerk:operator:admin",
            user_id="operator",
            role="aiwerk_admin",
            display_name="Example Operator",
        ),
    )

    assert identity == {"name": "Example", "context": "admin"}


def test_unknown_actor_gets_neutral_greeting_identity():
    identity = web_server._assistant_greeting_identity(
        SimpleNamespace(state=SimpleNamespace(session=None)),
    )

    assert identity == {"name": None, "context": "unknown"}
