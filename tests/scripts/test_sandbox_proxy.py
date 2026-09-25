import importlib.util
import os
import socket
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from unittest.mock import Mock

import pytest

PROXY_PATH = Path(__file__).parents[2] / "scripts" / "sandbox" / "proxy.py"
STAGE2_PATH = Path(__file__).parents[2] / "scripts" / "sandbox" / "stage2-run.sh"


def load_proxy(tmp_path, monkeypatch):
    root = tmp_path / "http"
    certs = tmp_path / "certs"
    root.mkdir()
    certs.mkdir()
    real_ca = certs / "real-ca.pem"
    real_ca.write_text("real-ca\n", encoding="utf-8")
    (certs / "ca.pem").write_text("sandbox-ca\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [str(PROXY_PATH), str(root), str(certs), str(real_ca)])
    name = f"sandbox_proxy_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, PROXY_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, root


def test_non_fixture_connect_uses_transparent_tunnel(tmp_path, monkeypatch):
    proxy, _ = load_proxy(tmp_path, monkeypatch)
    tunnel = Mock()
    monkeypatch.setattr(proxy, "tunnel_connect", tunnel, raising=False)
    monkeypatch.setattr(
        proxy,
        "cert_for",
        Mock(side_effect=AssertionError("non-fixture hosts must not be MITM intercepted")),
    )
    client = Mock()

    proxy.handle_connect(client, "registry.npmjs.org:443")

    tunnel.assert_called_once_with(client, "registry.npmjs.org", 443)


def test_fixture_host_connect_keeps_mitm_interception(tmp_path, monkeypatch):
    proxy, root = load_proxy(tmp_path, monkeypatch)
    (root / "raw.githubusercontent.com").mkdir()
    intercept = Mock()
    monkeypatch.setattr(proxy, "intercept_connect", intercept, raising=False)
    tunnel = Mock()
    monkeypatch.setattr(proxy, "tunnel_connect", tunnel, raising=False)
    client = Mock()

    proxy.handle_connect(client, "raw.githubusercontent.com:443")

    intercept.assert_called_once_with(client, "raw.githubusercontent.com", 443)
    tunnel.assert_not_called()


def test_connect_host_cannot_escape_fixture_root(tmp_path, monkeypatch):
    proxy, _ = load_proxy(tmp_path, monkeypatch)
    tunnel = Mock()
    intercept = Mock()
    monkeypatch.setattr(proxy, "tunnel_connect", tunnel)
    monkeypatch.setattr(proxy, "intercept_connect", intercept)
    client = Mock()

    proxy.handle_connect(client, "../certs:443")

    tunnel.assert_called_once_with(client, "../certs", 443)
    intercept.assert_not_called()


def test_tunnel_connect_clears_stream_timeouts_after_success(tmp_path, monkeypatch):
    proxy, _ = load_proxy(tmp_path, monkeypatch)
    events = []
    client = Mock()
    upstream = Mock()
    upstream.__enter__ = Mock(return_value=upstream)
    upstream.__exit__ = Mock(return_value=False)
    client.sendall.side_effect = lambda payload: events.append(("connected", payload))
    client.settimeout.side_effect = lambda value: events.append(("client-timeout", value))
    upstream.settimeout.side_effect = lambda value: events.append(("upstream-timeout", value))
    monkeypatch.setattr(
        proxy.socket,
        "create_connection",
        lambda address, timeout: events.append(("connect", address, timeout)) or upstream,
    )
    monkeypatch.setattr(
        proxy,
        "relay_tunnel",
        lambda left, right: events.append(("relay", left, right)),
    )

    proxy.tunnel_connect(client, "registry.npmjs.org", 443)

    assert events == [
        ("connect", ("registry.npmjs.org", 443), proxy.UPSTREAM_TIMEOUT_SECONDS),
        ("connected", b"HTTP/1.1 200 Connection Established\r\n\r\n"),
        ("client-timeout", None),
        ("upstream-timeout", None),
        ("relay", client, upstream),
    ]


def test_tunnel_connect_does_not_acknowledge_failed_upstream(tmp_path, monkeypatch):
    proxy, _ = load_proxy(tmp_path, monkeypatch)
    client = Mock()
    monkeypatch.setattr(
        proxy.socket,
        "create_connection",
        Mock(side_effect=OSError("unreachable")),
    )

    with pytest.raises(OSError, match="unreachable"):
        proxy.tunnel_connect(client, "registry.npmjs.org", 443)

    client.sendall.assert_not_called()


def test_trust_bundle_contains_fixture_and_public_authorities(tmp_path, monkeypatch):
    proxy, _ = load_proxy(tmp_path, monkeypatch)

    bundle = proxy.build_trust_bundle()

    assert bundle.read_text(encoding="utf-8") == "sandbox-ca\nreal-ca\n"


def test_proxy_publishes_trust_bundle_before_listening(tmp_path, monkeypatch):
    proxy, _ = load_proxy(tmp_path, monkeypatch)
    events = []

    class StopMain(Exception):
        pass

    class FakeServer:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def setsockopt(self, *args):
            events.append("setsockopt")

        def bind(self, address):
            events.append(("bind", address))

        def listen(self):
            events.append("listen")

        def accept(self):
            raise StopMain

    monkeypatch.setattr(
        proxy,
        "build_trust_bundle",
        lambda: events.append("bundle") or proxy.TRUST_BUNDLE,
    )
    monkeypatch.setattr(
        proxy.socket,
        "socket",
        lambda *args: events.append("socket") or FakeServer(),
    )

    with pytest.raises(StopMain):
        proxy.main()

    assert events[:5] == [
        "bundle",
        "socket",
        "setsockopt",
        ("bind", proxy.LISTEN_ADDRESS),
        "listen",
    ]


@pytest.mark.linux_only
def test_stage2_exports_combined_bundle_to_every_tls_client(tmp_path):
    sandbox = tmp_path / "sandbox"
    for relative in ("root/logs", "root/usr/local", "home", "etc"):
        (sandbox / relative).mkdir(parents=True, exist_ok=True)
    (sandbox / "root/logs/slirp.ready").write_text("ready\n", encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    captured = tmp_path / "bwrap-args.txt"
    bwrap = fake_bin / "bwrap"
    bwrap.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$BWRAP_ARGS\"\n",
        encoding="utf-8",
    )
    bwrap.chmod(0o700)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "BWRAP_ARGS": str(captured),
        "DEV_SANDBOX_ROOT": str(sandbox),
        "DEV_SANDBOX_BASH": "/bin/bash",
        "DEV_SANDBOX_INTERACTIVE": "false",
        "DEV_SANDBOX_USER": "sandbox-user",
        "DEV_SANDBOX_HOME": "/home/sandbox-user",
    }

    result = subprocess.run(
        ["bash", str(STAGE2_PATH), "true"],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    args = captured.read_text(encoding="utf-8").splitlines()
    setenv = {
        (args[index + 1], args[index + 2])
        for index, value in enumerate(args[:-2])
        if value == "--setenv"
    }
    bundle = "/work/certs/ca-bundle.pem"
    assert {
        ("CURL_CA_BUNDLE", bundle),
        ("SSL_CERT_FILE", bundle),
        ("GIT_SSL_CAINFO", bundle),
        ("NODE_EXTRA_CA_CERTS", bundle),
    } <= setenv


def test_transparent_tunnel_relays_both_directions(tmp_path, monkeypatch):
    proxy, _ = load_proxy(tmp_path, monkeypatch)
    client_app, proxy_client = socket.socketpair()
    proxy_upstream, server_app = socket.socketpair()
    sockets = (client_app, proxy_client, proxy_upstream, server_app)
    for sock in sockets:
        sock.settimeout(2)
    worker = threading.Thread(
        target=proxy.relay_tunnel, args=(proxy_client, proxy_upstream), daemon=True
    )
    worker.start()
    try:
        client_app.sendall(b"request")
        assert server_app.recv(7) == b"request"
        server_app.sendall(b"response")
        assert client_app.recv(8) == b"response"
    finally:
        client_app.close()
        server_app.close()
        worker.join(timeout=2)
        proxy_client.close()
        proxy_upstream.close()
    assert not worker.is_alive()
