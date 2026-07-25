"""Relay media client — gateway↔connector media plane (Phase 2). EXPERIMENTAL.

The relay wire contract carries media BY REFERENCE, never by value: an inbound
event's ``media_urls`` name connector re-hosted attachments
(``{connector}/relay/media/{id}``), and an outbound ``send_media`` op names a
``source_url`` the connector resolves back to bytes. This module is the
gateway-side HTTP client for that plane:

  - ``download(url)``  → GET a re-hosted attachment to a local temp file (the
    agent's vision/file tools consume LOCAL paths, matching every native
    adapter's inbound media behaviour).
  - ``upload(path)``   → POST local file bytes to ``/relay/media``; returns the
    ``/relay/media/{id}`` reference for a subsequent ``send_media`` op. This is
    how a locally-generated artifact (image_generate output, TTS voice note,
    a document) crosses to the connector WITHOUT the gateway needing a public
    URL.

Both requests present the SAME per-gateway signed bearer the WS upgrade uses
(``make_upgrade_token``, gateway/relay/auth.py — the channel authenticator; the
connector authenticates it with ``authenticateGatewayBearer`` on the mirrored
routes). Uploads are
per-gateway-owned on the connector (only this gateway can reference the id
back); downloads accept both this gateway's uploads and connector ingest
re-hosts.

Transport is stdlib ``urllib``/``http.client`` run in a thread executor (the
relay lane adds no HTTP client dependencies).
EXPERIMENTAL: may change without a deprecation cycle (docs/relay-connector-contract.md).
"""

from __future__ import annotations

import asyncio
import http.client
import ipaddress
import logging
import mimetypes
import os
import socket
import ssl
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

from gateway.relay.auth import make_upgrade_token

logger = logging.getLogger(__name__)

# Mirror the connector's MEDIA_MAX_BYTES (mediaStore.ts) so an oversized local
# artifact fails fast here instead of round-tripping to a connector 413.
MEDIA_MAX_BYTES = 25 * 1024 * 1024

_REQUEST_TIMEOUT_S = 30.0
_MAX_REDIRECTS = 3
_REDIRECT_STATUS_CODES = frozenset({301, 302, 303, 307, 308})
_CLOUD_METADATA_NETWORKS = (
    ipaddress.ip_network("169.254.169.254/32"),
    ipaddress.ip_network("169.254.170.2/32"),
    ipaddress.ip_network("100.100.100.200/32"),
    ipaddress.ip_network("168.63.129.16/32"),
    ipaddress.ip_network("fd00:ec2::254/128"),
)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Make redirects visible so every target can be revalidated."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _parsed_http_url(
    url: str,
) -> Optional[tuple[urllib.parse.SplitResult, tuple[str, str, int]]]:
    """Parse an uncredentialed HTTP(S) URL and return its normalized origin."""
    try:
        parsed = urllib.parse.urlsplit(url)
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "https"} or not parsed.hostname:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        port = parsed.port or (443 if scheme == "https" else 80)
        if not 1 <= port <= 65535:
            return None
        host = parsed.hostname.rstrip(".").lower()
        if not host:
            return None
        return parsed, (scheme, host, port)
    except (TypeError, ValueError):
        return None


def _is_public_address(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if any(ip in network for network in _CLOUD_METADATA_NETWORKS):
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return _is_public_address(str(ip.ipv4_mapped))
    return bool(
        ip.is_global
        and not ip.is_private
        and not ip.is_loopback
        and not ip.is_link_local
        and not ip.is_multicast
        and not ip.is_reserved
        and not ip.is_unspecified
    )


_Address = tuple[int, int, int, tuple]


def _resolved_addresses(
    host: str, port: int, *, require_public: bool
) -> tuple[_Address, ...]:
    """Resolve once and return connectable addresses that passed this hop's policy."""
    try:
        literal = ipaddress.ip_address(host)
        family = socket.AF_INET6 if literal.version == 6 else socket.AF_INET
        sockaddr = (str(literal), port, 0, 0) if family == socket.AF_INET6 else (str(literal), port)
        answers = [(family, socket.SOCK_STREAM, 0, "", sockaddr)]
    except ValueError:
        try:
            answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except (OSError, socket.gaierror):
            return ()

    resolved: list[_Address] = []
    seen: set[tuple[int, tuple]] = set()
    for family, socktype, proto, _canonname, sockaddr in answers:
        if family not in {socket.AF_INET, socket.AF_INET6} or not sockaddr:
            continue
        address = sockaddr[0]
        if not isinstance(address, str) or (require_public and not _is_public_address(address)):
            return ()
        key = (family, sockaddr)
        if key not in seen:
            seen.add(key)
            resolved.append((family, socktype, proto, sockaddr))
    return tuple(resolved)


def _same_ip(left: str, right: str) -> bool:
    try:
        left_ip = ipaddress.ip_address(left)
        right_ip = ipaddress.ip_address(right)
    except ValueError:
        return False
    if isinstance(left_ip, ipaddress.IPv6Address) and left_ip.ipv4_mapped:
        left_ip = left_ip.ipv4_mapped
    if isinstance(right_ip, ipaddress.IPv6Address) and right_ip.ipv4_mapped:
        right_ip = right_ip.ipv4_mapped
    return left_ip == right_ip


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """HTTP connection whose socket can only reach one prevalidated address."""

    def __init__(self, host: str, port: int, addresses: tuple[_Address, ...], timeout: float):
        super().__init__(host, port=port, timeout=timeout)
        self._addresses = addresses

    def connect(self) -> None:
        last_error: Optional[OSError] = None
        for family, socktype, proto, sockaddr in self._addresses:
            sock = None
            try:
                sock = socket.socket(family, socktype, proto)
                sock.settimeout(self.timeout)
                sock.connect(sockaddr)
                peer = sock.getpeername()
                if not peer or not _same_ip(peer[0], sockaddr[0]):
                    raise OSError("connected peer did not match validated address")
                self.sock = sock
                return
            except OSError as exc:
                last_error = exc
                if sock is not None:
                    sock.close()
        raise last_error or OSError("no validated address available")


class _PinnedHTTPSConnection(_PinnedHTTPConnection):
    """Pinned TCP with normal CA validation and the original hostname as SNI."""

    default_port = 443

    def __init__(
        self,
        host: str,
        port: int,
        addresses: tuple[_Address, ...],
        timeout: float,
    ) -> None:
        super().__init__(host, port, addresses, timeout)
        self._context = ssl.create_default_context()

    def connect(self) -> None:
        super().connect()
        assert self.sock is not None
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


class _PinnedResponse:
    def __init__(self, connection: http.client.HTTPConnection, response) -> None:
        self._connection = connection
        self._response = response

    def __getattr__(self, name):
        return getattr(self._response, name)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        try:
            self._response.close()
        finally:
            self._connection.close()


def _open_pinned_response(
    request: urllib.request.Request,
    addresses: tuple[_Address, ...],
    timeout: float,
):
    """Open one request without performing a second hostname resolution."""
    parsed = urllib.parse.urlsplit(request.full_url)
    host = parsed.hostname
    if not host:
        raise ValueError("request URL has no host")
    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    connection_cls = (
        _PinnedHTTPSConnection if parsed.scheme.lower() == "https" else _PinnedHTTPConnection
    )
    connection = connection_cls(host, port, addresses, timeout)
    path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    try:
        connection.request(
            request.get_method(),
            path,
            body=request.data,
            headers=dict(request.header_items()),
        )
        return _PinnedResponse(connection, connection.getresponse())
    except BaseException:
        connection.close()
        raise


def media_base_url(relay_dial_url: str) -> str:
    """Map the ``ws(s)://…/relay`` dial URL to the ``http(s)://…`` base.

    Same host derivation as ``_provision_url`` (gateway/relay/__init__.py):
    scheme ws→http / wss→https, trailing ``/relay`` stripped.
    """
    raw = (relay_dial_url or "").strip().rstrip("/")
    if raw.startswith("ws://"):
        raw = "http://" + raw[len("ws://") :]
    elif raw.startswith("wss://"):
        raw = "https://" + raw[len("wss://") :]
    if raw.endswith("/relay"):
        raw = raw[: -len("/relay")]
    return raw


class RelayMediaClient:
    """Authenticated client for the connector's ``/relay/media`` routes."""

    def __init__(
        self,
        base_url: str,
        gateway_id: Optional[str],
        secret: Optional[str],
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._gateway_id = gateway_id or ""
        self._secret = secret or ""
        parsed_base = _parsed_http_url(self._base_url)
        self._relay_origin = parsed_base[1] if parsed_base else None

    @property
    def enabled(self) -> bool:
        """True when the client can authenticate (per-gateway creds present)."""
        return bool(self._base_url and self._gateway_id and self._secret)

    def _bearer(self) -> str:
        return make_upgrade_token(self._gateway_id, self._secret)

    def is_relay_media_url(self, url: str) -> bool:
        """Is ``url`` a connector re-host reference (needs our bearer to GET)?"""
        candidate = _parsed_http_url(url)
        if not candidate or candidate[1] != self._relay_origin:
            return False
        decoded_path = urllib.parse.unquote(candidate[0].path)
        prefix = "/relay/media/"
        media_id = decoded_path[len(prefix) :] if decoded_path.startswith(prefix) else ""
        return bool(media_id and "/" not in media_id and "\\" not in media_id)

    def _validated_download_target(
        self, url: str
    ) -> Optional[tuple[str, tuple[_Address, ...]]]:
        """Apply URL policy and bind the hop to this exact DNS answer set."""
        candidate = _parsed_http_url(url)
        if not candidate:
            return None
        _parsed, (_scheme, host, port) = candidate
        if self.is_relay_media_url(url):
            if not self.enabled:
                return None
            addresses = _resolved_addresses(host, port, require_public=False)
            return ("relay", addresses) if addresses else None
        addresses = _resolved_addresses(host, port, require_public=True)
        return ("public", addresses) if addresses else None

    async def upload(
        self,
        file_path: str,
        *,
        mime: Optional[str] = None,
        filename: Optional[str] = None,
    ) -> Optional[str]:
        """POST local file bytes to ``/relay/media``; return the reference URL.

        Returns the ``{base}/relay/media/{id}`` reference for a ``send_media``
        op's ``source_url``, or None on any failure (callers fall back to their
        pre-media behaviour — media delivery is best-effort by design).
        """
        if not self.enabled:
            return None
        path = Path(file_path)
        try:
            data = path.read_bytes()
        except OSError:
            logger.warning("relay media upload: cannot read %s", file_path)
            return None
        if not data or len(data) > MEDIA_MAX_BYTES:
            logger.warning(
                "relay media upload: %s size %d outside (0, %d]",
                file_path,
                len(data),
                MEDIA_MAX_BYTES,
            )
            return None
        content_type = (
            mime
            or mimetypes.guess_type(filename or path.name)[0]
            or "application/octet-stream"
        )
        headers = {
            "Authorization": f"Bearer {self._bearer()}",
            "Content-Type": content_type,
            "X-Media-Filename": (filename or path.name)[:255],
        }
        url = f"{self._base_url}/relay/media"

        def _post() -> Optional[str]:
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            opener = urllib.request.build_opener(_NoRedirectHandler())
            try:
                # Upload redirects are deliberately not followed: the request body and
                # bearer must never be replayed to a different origin.
                with opener.open(req, timeout=_REQUEST_TIMEOUT_S) as resp:
                    import json

                    body = json.loads(resp.read().decode("utf-8"))
                    media_id = body.get("id")
                    if not media_id:
                        return None
                    return f"{self._base_url}/relay/media/{media_id}"
            except (urllib.error.URLError, ValueError, OSError) as exc:
                logger.warning("relay media upload failed: %s", exc)
                return None

        return await asyncio.get_running_loop().run_in_executor(None, _post)

    async def download(self, url: str, *, suggested_name: Optional[str] = None) -> Optional[str]:
        """GET a re-hosted attachment to a local temp file; return its path.

        Connector relay-media URLs receive the gateway bearer. Other URLs must
        resolve exclusively to public addresses and are fetched without auth.
        Redirects are followed manually and the full policy is re-applied at
        every hop. DNS failures fail closed. Returns None on any failure.
        """
        if not url:
            return None

        def _save_response(resp, current_url: str) -> Optional[str]:
            length = int(resp.headers.get("Content-Length") or 0)
            if length > MEDIA_MAX_BYTES:
                logger.warning("relay media download too large: %s", current_url)
                return None
            data = resp.read(MEDIA_MAX_BYTES + 1)
            if not data or len(data) > MEDIA_MAX_BYTES:
                return None
            # Extension: prefer content-disposition / suggested name, then MIME.
            name = suggested_name or ""
            if not name:
                cd = resp.headers.get("Content-Disposition") or ""
                if "filename=" in cd:
                    name = cd.split("filename=", 1)[1].strip().strip('"')
            ext = Path(name).suffix if name else ""
            if not ext:
                mime = (resp.headers.get("Content-Type") or "").split(";")[0]
                ext = mimetypes.guess_extension(mime) or ".bin"
            fd, tmp_path = tempfile.mkstemp(prefix="relay_media_", suffix=ext)
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            return tmp_path

        def _get() -> Optional[str]:
            current_url = url
            for redirect_count in range(_MAX_REDIRECTS + 1):
                target = self._validated_download_target(current_url)
                if target is None:
                    logger.warning("relay media download blocked by URL policy: %s", current_url)
                    return None
                kind, addresses = target
                headers = {}
                if kind == "relay":
                    headers["Authorization"] = f"Bearer {self._bearer()}"
                req = urllib.request.Request(current_url, headers=headers)
                try:
                    with _open_pinned_response(req, addresses, _REQUEST_TIMEOUT_S) as resp:
                        status = getattr(resp, "status", 200)
                        location = resp.headers.get("Location") if resp.headers else None
                        if status in _REDIRECT_STATUS_CODES:
                            if not location:
                                logger.warning(
                                    "relay media download redirect missing Location: %s",
                                    current_url,
                                )
                                return None
                            if redirect_count >= _MAX_REDIRECTS:
                                logger.warning(
                                    "relay media download redirect limit exceeded: %s", url
                                )
                                return None
                            current_url = urllib.parse.urljoin(current_url, location)
                            continue
                        if status >= 400:
                            logger.warning(
                                "relay media download failed for %s: HTTP %d",
                                current_url,
                                status,
                            )
                            return None
                        return _save_response(resp, current_url)
                except (
                    urllib.error.URLError,
                    http.client.HTTPException,
                    ValueError,
                    OSError,
                ) as exc:
                    logger.warning("relay media download failed for %s: %s", current_url, exc)
                    return None
            return None

        return await asyncio.get_running_loop().run_in_executor(None, _get)


__all__ = ["RelayMediaClient", "media_base_url", "MEDIA_MAX_BYTES"]
