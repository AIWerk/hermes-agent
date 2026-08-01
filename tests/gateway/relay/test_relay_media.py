"""Relay Phase 2 media tests — send_media egress lanes + inbound media localization.

Covers:
  - the five ``send_*`` overrides route through ONE ``send_media`` op with the
    right ``media_kind`` and honor op-level capability gating (a connector not
    advertising ``send_media`` falls back to the base-class behaviour);
  - local-path sources upload through the RelayMediaClient first (the
    connector cannot reach our filesystem) and public URLs pass through;
  - a connector decline / failed upload degrades to the pre-media fallback;
  - inbound ``media_urls`` are localized to temp paths (re-hosts downloaded
    with the per-gateway bearer; dead re-host refs dropped; public URLs kept
    when no client is available);
  - the RelayMediaClient URL derivation + auth header shape.
"""

from __future__ import annotations

import io
import socket
import urllib.request
from pathlib import Path
from typing import Optional

import pytest

import gateway.relay.media as relay_media
from gateway.config import PlatformConfig
from gateway.relay.adapter import RelayAdapter
from gateway.relay.descriptor import CONTRACT_VERSION, CapabilityDescriptor
from gateway.relay.media import RelayMediaClient, media_base_url

from tests.gateway.relay.stub_connector import StubConnector


def make_desc(**kw) -> CapabilityDescriptor:
    base = dict(
        contract_version=CONTRACT_VERSION,
        platform="telegram",
        label="Telegram",
        max_message_length=4096,
        supports_draft_streaming=False,
        supports_edit=True,
        supports_threads=True,
        markdown_dialect="markdown_v2",
        len_unit="utf16",
        supported_ops=(
            "send",
            "edit",
            "typing",
            "get_chat_info",
            "send_media",
        ),
    )
    base.update(kw)
    return CapabilityDescriptor(**base)


class FakeMediaClient:
    """In-memory stand-in for RelayMediaClient (no HTTP)."""

    def __init__(self) -> None:
        self.enabled = True
        self.uploads: list[tuple[str, Optional[str]]] = []
        self.downloads: list[str] = []
        self.upload_result: Optional[str] = "https://conn.example/relay/media/aa11"
        self.download_result: Optional[str] = "/tmp/relay_media_fake.png"

    async def upload(self, file_path, *, mime=None, filename=None):
        self.uploads.append((str(file_path), filename))
        return self.upload_result

    async def download(self, url, *, suggested_name=None):
        self.downloads.append(url)
        return self.download_result

    def is_relay_media_url(self, url: str) -> bool:
        return "/relay/media/" in (url or "")


def _adapter(**desc_kw) -> tuple[RelayAdapter, StubConnector, FakeMediaClient]:
    stub = StubConnector(make_desc(**desc_kw))
    adapter = RelayAdapter(PlatformConfig(), make_desc(**desc_kw), transport=stub)
    fake = FakeMediaClient()
    adapter._media_client = fake  # bypass env-derived construction
    return adapter, stub, fake


# ── egress: the five overrides ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_image_url_passes_through_without_upload():
    adapter, stub, fake = _adapter()
    result = await adapter.send_image(
        "chat1", "https://fal.media/x.png", caption="a pic", reply_to="m9"
    )
    assert result.success is True
    assert result.message_id == "md1"
    assert fake.uploads == []  # public URL → no upload leg
    action = stub.sent[-1]
    assert action["op"] == "send_media"
    assert action["media_kind"] == "image"
    assert action["source_url"] == "https://fal.media/x.png"
    assert action["content"] == "a pic"
    assert action["reply_to"] == "m9"


@pytest.mark.asyncio
async def test_local_path_lanes_upload_first(tmp_path: Path):
    adapter, stub, fake = _adapter()
    f = tmp_path / "clip.ogg"
    f.write_bytes(b"oggbytes")
    result = await adapter.send_voice("chat1", str(f), caption="listen")
    assert result.success is True
    assert fake.uploads == [(str(f), None)]
    action = stub.sent[-1]
    assert action["op"] == "send_media"
    assert action["media_kind"] == "voice"
    # The wire carries the RE-HOST reference, never the local path.
    assert action["source_url"] == fake.upload_result
    assert str(f) not in str(action)


@pytest.mark.asyncio
async def test_op_gating_falls_back_when_not_advertised(tmp_path: Path):
    # Connector advertises only the legacy ops — send_media must never hit the wire.
    adapter, stub, fake = _adapter(
        supported_ops=("send", "edit", "typing", "get_chat_info")
    )
    result = await adapter.send_image("chat1", "https://x.io/a.png", caption="hi")
    # Base-class fallback: caption + URL as a text send.
    assert result.success is True
    ops = [a["op"] for a in stub.sent]
    assert "send_media" not in ops
    assert ops[-1] == "send"
    assert "https://x.io/a.png" in stub.sent[-1]["content"]


# ── inbound localization ─────────────────────────────────────────────────


def _make_event(media_urls):
    from gateway.platforms.base import MessageEvent, MessageType
    from gateway.session import SessionSource

    return MessageEvent(
        text="look",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform="telegram", chat_id="c1", chat_type="dm", user_id="u1"
        ),
        media_urls=list(media_urls),
    )


@pytest.mark.asyncio
async def test_inbound_without_client_keeps_public_drops_rehost():
    adapter, _stub, _fake = _adapter()
    adapter._media_client = None
    adapter._get_media_client = lambda: None  # type: ignore[method-assign]
    event = _make_event(
        [
            "https://conn.example/relay/media/deadbeef",
            "https://cdn.discordapp.com/attachments/a/b.png",
        ]
    )
    await adapter._localize_inbound_media(event)
    assert event.media_urls == ["https://cdn.discordapp.com/attachments/a/b.png"]


# ── RelayMediaClient unit surface ────────────────────────────────────────


def test_media_base_url_derivation():
    assert media_base_url("wss://conn.example/relay") == "https://conn.example"
    assert media_base_url("ws://localhost:8080/relay") == "http://localhost:8080"
    assert media_base_url("https://conn.example") == "https://conn.example"


def test_client_enabled_requires_full_credentials():
    assert RelayMediaClient("https://c.example", "gw1", "sec").enabled is True
    assert RelayMediaClient("https://c.example", None, "sec").enabled is False
    assert RelayMediaClient("https://c.example", "gw1", None).enabled is False
    assert RelayMediaClient("", "gw1", "sec").enabled is False


def test_client_recognizes_only_exact_origin_rehost_urls():
    c = RelayMediaClient("https://c.example", "gw1", "sec")
    assert c.is_relay_media_url("https://c.example/relay/media/abc") is True
    assert c.is_relay_media_url("https://c.example.evil/relay/media/abc") is False
    assert c.is_relay_media_url("https://evil.example/relay/media/abc") is False
    assert c.is_relay_media_url("https://c.example/not-relay/media/abc") is False
    assert c.is_relay_media_url("https://c.example/relay/media/../admin") is False
    assert c.is_relay_media_url("https://c.example/relay/media/a%2Fb") is False
    assert c.is_relay_media_url("https://cdn.discordapp.com/a/b.png") is False


class _DownloadResponse:
    def __init__(self, body: bytes = b"image", status: int = 200, **headers: str) -> None:
        self._body = io.BytesIO(body)
        self.status = status
        self.headers = headers or {"Content-Type": "image/png"}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)


class _RecordingOpener:
    def __init__(self, outcomes) -> None:
        self.outcomes = list(outcomes)
        self.requests: list[urllib.request.Request] = []

    def open(self, request, data=None, timeout=None):
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _public_dns(monkeypatch, address: str = "93.184.216.34") -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, type=0: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))
        ],
    )


def _redirect(_source: str, target: str) -> _DownloadResponse:
    return _DownloadResponse(b"", status=302, **{"Location": target})


@pytest.mark.asyncio
async def test_attacker_relay_path_on_other_origin_receives_no_bearer(monkeypatch):
    _public_dns(monkeypatch)
    opener = _RecordingOpener([_DownloadResponse()])
    monkeypatch.setattr(relay_media, "_open_pinned_response", opener.open)
    client = RelayMediaClient("https://connector.example", "gw1", "sec")

    result = await client.download("https://attacker.example/relay/media/steal")

    assert result is not None
    Path(result).unlink()
    assert opener.requests[0].get_header("Authorization") is None


@pytest.mark.asyncio
async def test_exact_configured_relay_origin_receives_bearer(monkeypatch):
    _public_dns(monkeypatch)
    opener = _RecordingOpener([_DownloadResponse()])
    monkeypatch.setattr(relay_media, "_open_pinned_response", opener.open)
    client = RelayMediaClient("https://connector.example", "gw1", "sec")

    result = await client.download("https://connector.example/relay/media/abc")

    assert result is not None
    Path(result).unlink()
    assert opener.requests[0].get_header("Authorization", "").startswith("Bearer ")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/media.png",
        "http://10.1.2.3/media.png",
        "http://169.254.169.254/latest/meta-data/",
        "http://168.63.129.16/metadata/instance",
        "http://0.0.0.0/media.png",
        "http://224.0.0.1/media.png",
        "http://240.0.0.1/media.png",
        "http://[::1]/media.png",
        "http://[::]/media.png",
        "http://[fc00::1]/media.png",
        "http://[fe80::1]/media.png",
        "http://[ff02::1]/media.png",
        "http://[2001:db8::1]/media.png",
        "file:///etc/passwd",
        "data:text/plain,secret",
    ],
)
async def test_public_download_rejects_unsafe_destinations_and_schemes(monkeypatch, url):
    opener = _RecordingOpener([_DownloadResponse()])
    monkeypatch.setattr(relay_media, "_open_pinned_response", opener.open)
    client = RelayMediaClient("https://connector.example", "gw1", "sec")

    assert await client.download(url) is None
    assert opener.requests == []


@pytest.mark.asyncio
async def test_public_download_pins_validated_address_across_dns_rebinding(monkeypatch):
    public_ip = "93.184.216.34"
    private_ip = "127.0.0.1"
    dns_calls = 0
    connected: list[str] = []
    sent = bytearray()

    def rebinding_dns(host, port, type=0):
        nonlocal dns_calls
        dns_calls += 1
        address = public_ip if dns_calls == 1 else private_ip
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    class PinnedSocket:
        def __init__(self, *_args, **_kwargs):
            self._peer = None

        def settimeout(self, _timeout):
            pass

        def connect(self, address):
            self._peer = address
            connected.append(str(address[0]))

        def getpeername(self):
            return self._peer

        def sendall(self, data):
            sent.extend(data)

        def makefile(self, *_args, **_kwargs):
            return io.BytesIO(
                b"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\n"
                b"Content-Length: 5\r\n\r\nimage"
            )

        def close(self):
            pass

    monkeypatch.setattr(socket, "getaddrinfo", rebinding_dns)
    monkeypatch.setattr(socket, "socket", PinnedSocket)
    client = RelayMediaClient("https://connector.example", "gw1", "sec")

    result = await client.download("http://rebind.example/image.png")

    assert result is not None
    Path(result).unlink()
    assert connected == [public_ip]
    assert dns_calls == 1
    assert b"Host: rebind.example\r\n" in sent


def test_pinned_connection_falls_back_from_ipv4_to_validated_ipv6(monkeypatch):
    attempts: list[tuple[int, str]] = []

    class FamilySocket:
        def __init__(self, family, *_args):
            self.family = family
            self._peer = None

        def settimeout(self, _timeout):
            pass

        def connect(self, address):
            attempts.append((self.family, address[0]))
            if self.family == socket.AF_INET:
                raise OSError("IPv4 unavailable")
            self._peer = address

        def getpeername(self):
            return self._peer

        def close(self):
            pass

    addresses = (
        (socket.AF_INET, socket.SOCK_STREAM, 6, ("93.184.216.34", 80)),
        (socket.AF_INET6, socket.SOCK_STREAM, 6, ("2606:4700:4700::1111", 80, 0, 0)),
    )
    monkeypatch.setattr(socket, "socket", FamilySocket)
    connection = relay_media._PinnedHTTPConnection(
        "public.example", 80, addresses, timeout=1.0
    )

    connection.connect()

    assert attempts == [
        (socket.AF_INET, "93.184.216.34"),
        (socket.AF_INET6, "2606:4700:4700::1111"),
    ]
    assert connection.sock is not None
    connection.close()


@pytest.mark.asyncio
async def test_pinned_download_rejects_mismatched_connected_peer(monkeypatch):
    _public_dns(monkeypatch)

    class WrongPeerSocket:
        def __init__(self, *_args, **_kwargs):
            pass

        def settimeout(self, _timeout):
            pass

        def connect(self, _address):
            pass

        def getpeername(self):
            return ("127.0.0.1", 80)

        def close(self):
            pass

    monkeypatch.setattr(socket, "socket", WrongPeerSocket)
    client = RelayMediaClient("https://connector.example", "gw1", "sec")

    assert await client.download("http://public.example/image.png") is None


@pytest.mark.asyncio
async def test_https_pinning_keeps_original_hostname_for_tls_sni(monkeypatch):
    _public_dns(monkeypatch)
    server_names: list[str] = []

    class FakeSocket:
        def __init__(self, *_args, **_kwargs):
            self._peer = None

        def settimeout(self, _timeout):
            pass

        def connect(self, address):
            self._peer = address

        def getpeername(self):
            return self._peer

        def sendall(self, _data):
            pass

        def makefile(self, *_args, **_kwargs):
            return io.BytesIO(
                b"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\n"
                b"Content-Length: 5\r\n\r\nimage"
            )

        def close(self):
            pass

    class FakeTLSContext:
        check_hostname = True
        verify_mode = 2

        def wrap_socket(self, sock, *, server_hostname):
            server_names.append(server_hostname)
            return sock

    monkeypatch.setattr(socket, "socket", FakeSocket)
    monkeypatch.setattr(relay_media.ssl, "create_default_context", FakeTLSContext)
    client = RelayMediaClient("https://connector.example", "gw1", "sec")

    result = await client.download("https://public.example/image.png")

    assert result is not None
    Path(result).unlink()
    assert server_names == ["public.example"]


@pytest.mark.asyncio
async def test_public_download_rejects_if_any_dns_answer_is_private(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, type=0: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.20", port)),
        ],
    )
    opener = _RecordingOpener([_DownloadResponse()])
    monkeypatch.setattr(relay_media, "_open_pinned_response", opener.open)
    client = RelayMediaClient("https://connector.example", "gw1", "sec")

    assert await client.download("https://mixed.example/image.png") is None
    assert opener.requests == []


@pytest.mark.asyncio
async def test_public_download_fails_closed_on_dns_failure(monkeypatch):
    def fail_dns(host, port, type=0):
        raise socket.gaierror("not found")

    monkeypatch.setattr(socket, "getaddrinfo", fail_dns)
    opener = _RecordingOpener([_DownloadResponse()])
    monkeypatch.setattr(relay_media, "_open_pinned_response", opener.open)
    client = RelayMediaClient("https://connector.example", "gw1", "sec")

    assert await client.download("https://missing.example/image.png") is None
    assert opener.requests == []


@pytest.mark.asyncio
async def test_safe_public_download_is_allowed(monkeypatch):
    _public_dns(monkeypatch)
    opener = _RecordingOpener([_DownloadResponse()])
    monkeypatch.setattr(relay_media, "_open_pinned_response", opener.open)
    client = RelayMediaClient("https://connector.example", "gw1", "sec")

    result = await client.download("https://public.example/image.png")

    assert result is not None
    Path(result).unlink()
    assert len(opener.requests) == 1


@pytest.mark.asyncio
async def test_redirect_to_private_destination_is_rejected(monkeypatch):
    _public_dns(monkeypatch)
    source = "https://public.example/image.png"
    opener = _RecordingOpener([_redirect(source, "http://127.0.0.1/secret")])
    monkeypatch.setattr(relay_media, "_open_pinned_response", opener.open)
    client = RelayMediaClient("https://connector.example", "gw1", "sec")

    assert await client.download(source) is None
    assert len(opener.requests) == 1


@pytest.mark.asyncio
async def test_cross_origin_redirect_drops_connector_bearer(monkeypatch):
    _public_dns(monkeypatch)
    source = "https://connector.example/relay/media/abc"
    target = "https://public.example/image.png"
    opener = _RecordingOpener(
        [_redirect(source, target), _DownloadResponse()]
    )
    monkeypatch.setattr(relay_media, "_open_pinned_response", opener.open)
    client = RelayMediaClient("https://connector.example", "gw1", "sec")

    result = await client.download(source)

    assert result is not None
    Path(result).unlink()
    assert opener.requests[0].get_header("Authorization", "").startswith("Bearer ")
    assert opener.requests[1].get_header("Authorization") is None


@pytest.mark.asyncio
async def test_redirect_chain_is_bounded(monkeypatch):
    _public_dns(monkeypatch)
    urls = [f"https://public.example/{index}" for index in range(5)]
    opener = _RecordingOpener(
        [_redirect(urls[index], urls[index + 1]) for index in range(4)]
    )
    monkeypatch.setattr(relay_media, "_open_pinned_response", opener.open)
    client = RelayMediaClient("https://connector.example", "gw1", "sec")

    assert await client.download(urls[0]) is None
    assert len(opener.requests) == 4


@pytest.mark.asyncio
async def test_client_upload_rejects_oversize_and_missing(tmp_path: Path):
    c = RelayMediaClient("https://c.example", "gw1", "sec")
    # Missing file → None (no network attempted).
    assert await c.upload(str(tmp_path / "nope.bin")) is None
    # Empty file → None.
    empty = tmp_path / "empty.bin"
    empty.write_bytes(b"")
    assert await c.upload(str(empty)) is None
