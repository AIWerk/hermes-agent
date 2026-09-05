"""Regression coverage for fail-closed custom endpoint credential writes."""

from __future__ import annotations

import pytest


def _body(**overrides):
    from hermes_cli.web_server import CustomEndpointUpdate

    values = {
        "id": "managed-proxy",
        "name": "Managed Proxy",
        "base_url": "https://proxy.example/v1",
        "model": "proxy/model",
        "api_key": "new-key",
    }
    values.update(overrides)
    return CustomEndpointUpdate(**values)


def test_custom_endpoint_rejects_credential_write_refusal(monkeypatch) -> None:
    import hermes_cli.web_server as web_server

    config = {"providers": {}}
    monkeypatch.setattr(web_server, "save_env_value", lambda _key, _value: False)

    with pytest.raises(RuntimeError, match="credential persistence refused"):
        web_server._write_custom_endpoint(config, _body())

    assert config == {"providers": {}}


def test_custom_endpoint_rejects_credential_removal_refusal(monkeypatch) -> None:
    import hermes_cli.web_server as web_server

    env_var = web_server.custom_endpoint_key_env("managed-proxy")
    config = {
        "providers": {
            "managed-proxy": {
                "name": "Managed Proxy",
                "base_url": "https://proxy.example/v1",
                "model": "proxy/model",
                "key_env": env_var,
            }
        }
    }
    monkeypatch.setattr(web_server, "load_env", lambda: {env_var: "old"})
    monkeypatch.setattr(web_server, "remove_env_value", lambda _key: False)

    with pytest.raises(RuntimeError, match="credential removal refused"):
        web_server._write_custom_endpoint(config, _body(api_key=""))
