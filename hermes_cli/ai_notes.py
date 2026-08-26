"""AI Notes static HTML publishing support.

This module implements the runtime-safe core used by the publish_ai_note tool
and by AIWerk/base-agent verifiers. It deliberately only writes static HTML into
an explicitly configured publish root and returns the configured public URL.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import unicodedata
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse


_LOCALHOST_RE = re.compile(
    r"(?i)\b(?:https?://)?(?:localhost|127(?:\.\d{1,3}){3}|0\.0\.0\.0|\[::1\])(?::\d+)?\b"
)
_PRIVATE_URL_RE = re.compile(
    r"(?i)\bhttps?://(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})(?::\d+)?\b"
)
_FORBIDDEN_PATH_RE = re.compile(r"(?i)(?:file://|MEDIA:|/home/[^\s'\"<>]+|/Users/[^\s'\"<>]+|C:\\Users\\[^\s'\"<>]+)")
_SECRET_LIKE_RE = re.compile(
    r"(?i)(?:api[_-]?key|secret|password|passwd|token)\s*[:=]\s*['\"]?[A-Za-z0-9_./+=:-]{8,}"
)
_OPENAI_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9][A-Za-z0-9_-]{8,}\b")
_ABSOLUTE_HTTP_URL_RE = re.compile(r"(?i)\bhttps?://[^\s'\"<>]+")
_PROTOCOL_RELATIVE_URL_RE = re.compile(r"(?<!:)//[^\s'\"<>),]+")
_URL_ATTRS = {"href", "src", "srcset", "style", "action", "formaction", "poster", "data", "cite"}
_STATIC_HTML_TAGS = {
    "a", "abbr", "article", "aside", "b", "blockquote", "body", "br", "caption",
    "cite", "code", "col", "colgroup", "dd", "del", "details", "dfn", "div", "dl",
    "dt", "em", "figcaption", "figure", "footer", "h1", "h2", "h3", "h4", "h5",
    "h6", "head", "header", "hr", "html", "i", "img", "ins", "kbd", "li", "main",
    "mark", "meta", "nav", "ol", "p", "picture", "pre", "q", "s", "samp", "section",
    "small", "source", "span", "strong", "sub", "summary", "sup", "table",
    "tbody", "td", "tfoot", "th", "thead", "time", "title", "tr", "u", "ul", "var",
}
_STATIC_GLOBAL_ATTRS = {"class", "dir", "hidden", "id", "lang", "role", "title"}
_STATIC_TAG_ATTRS = {
    "a": {"download", "href", "referrerpolicy", "rel", "target"},
    "blockquote": {"cite"}, "q": {"cite"},
    "col": {"span", "width"}, "colgroup": {"span", "width"},
    "details": {"open"},
    "img": {"alt", "decoding", "height", "loading", "referrerpolicy", "sizes", "src", "srcset", "width"},
    "ins": {"cite", "datetime"}, "del": {"cite", "datetime"},
    "li": {"value"}, "ol": {"reversed", "start", "type"},
    "meta": {"charset", "content", "name"},
    "source": {"height", "media", "sizes", "src", "srcset", "type", "width"},
    "style": {"media", "type"},
    "td": {"colspan", "headers", "rowspan"},
    "th": {"abbr", "colspan", "headers", "rowspan", "scope"},
    "time": {"datetime"},
}
_ACTIVE_VALUE_RE = re.compile(r"(?i)(?:javascript|vbscript|data|blob)\s*:|expression\s*\(|-moz-binding|behavior\s*:")


class _StaticHtmlValidator(HTMLParser):
    """Fail closed unless markup is inert, supported static HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._validate(tag, attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._validate(tag, attrs)

    def _validate(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag not in _STATIC_HTML_TAGS:
            raise ValueError("AI Notes HTML contains active or unsupported HTML")
        allowed = _STATIC_TAG_ATTRS.get(tag, set())
        for raw_name, value in attrs:
            name = raw_name.lower()
            if name.startswith("on") or name == "srcdoc":
                raise ValueError("AI Notes HTML contains active or unsupported HTML")
            if (
                name not in _STATIC_GLOBAL_ATTRS
                and name not in allowed
                and not name.startswith("aria-")
                and not name.startswith("data-")
            ):
                raise ValueError("AI Notes HTML contains active or unsupported HTML")
            if value:
                canonical_value = re.sub(r"[\x00-\x20\x7f]+", "", value)
                if _ACTIVE_VALUE_RE.search(canonical_value):
                    raise ValueError("AI Notes HTML contains active or unsupported HTML")


class _HtmlUrlCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.urls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._collect(attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._collect(attrs)

    def _collect(self, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if value and name.lower() in _URL_ATTRS:
                self.urls.append(value.strip())


def _parse_network_url(raw_url: str):
    if raw_url.startswith("//"):
        return urlparse("https:" + raw_url)
    return urlparse(raw_url)


def sanitize_slug(value: str) -> str:
    """Return a URL/file safe slug with no path traversal surface."""
    normalized = unicodedata.normalize("NFKD", value or "")
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    lowered = ascii_text.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    return slug[:80].strip("-") or "note"


def _configured_root(raw_root: str | None) -> Path:
    if not raw_root:
        raise ValueError("AI Notes publish_root is required when publishing is enabled")
    return Path(raw_root).expanduser().resolve()


def _host_is_global(host: str) -> bool:
    if not host:
        return False
    trimmed = host.strip("[]")
    try:
        return ipaddress.ip_address(trimmed).is_global
    except ValueError:
        pass
    if trimmed.lower() in {"localhost", "localhost.localdomain"}:
        return False
    try:
        infos = socket.getaddrinfo(trimmed, None)
    except socket.gaierror:
        # Do not block syntactically-valid public hostnames just because DNS is
        # not resolvable in an offline/test environment. Obvious localhost names
        # were handled above.
        return True
    addresses = {item[4][0] for item in infos if item and item[4]}
    if not addresses:
        return False
    for address in addresses:
        try:
            if not ipaddress.ip_address(address).is_global:
                return False
        except ValueError:
            return False
    return True


def _join_under_root(root: Path, rel_parts: list[str]) -> Path:
    target = root.joinpath(*rel_parts).resolve()
    if target != root and root not in target.parents:
        raise ValueError("AI Notes publish path escaped publish_root")
    return target


def _validate_public_base_url(raw_url: str, *, allow_local: bool = False) -> str:
    url = (raw_url or "").strip().rstrip("/")
    if not url:
        raise ValueError("AI Notes public_base_url is required")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("AI Notes public_base_url must be an HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("AI Notes public_base_url must not include credentials, query, or fragment")
    if not allow_local and not _host_is_global(parsed.hostname or ""):
        raise ValueError("AI Notes public_base_url must use a public/global host")
    return url


def _iter_html_http_urls(html: str) -> list[str]:
    collector = _HtmlUrlCollector()
    try:
        collector.feed(html)
    except Exception:
        # HTMLParser is forgiving, but keep regex fallback as defense in depth.
        pass
    urls = list(collector.urls)
    urls.extend(match.group(0).rstrip(").,;") for match in _ABSOLUTE_HTTP_URL_RE.finditer(html))
    urls.extend(match.group(0).rstrip(").,;") for match in _PROTOCOL_RELATIVE_URL_RE.finditer(html))
    return urls


def _validate_embedded_urls_safe(html: str) -> None:
    for raw_url in _iter_html_http_urls(html):
        parsed = _parse_network_url(raw_url)
        if parsed.scheme.lower() not in {"http", "https"}:
            continue
        if not _host_is_global(parsed.hostname or ""):
            raise ValueError("AI Notes HTML contains forbidden non-public URL")


def _validate_html_safe_to_publish(html: str) -> None:
    if not isinstance(html, str) or not html.strip():
        raise ValueError("AI Notes HTML content is required")
    validator = _StaticHtmlValidator()
    try:
        validator.feed(html)
        validator.close()
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("AI Notes HTML contains active or unsupported HTML") from exc
    checks = [
        (_FORBIDDEN_PATH_RE, "local filesystem or MEDIA path"),
        (_LOCALHOST_RE, "localhost/private service URL"),
        (_PRIVATE_URL_RE, "private network URL"),
        (_SECRET_LIKE_RE, "secret-like assignment"),
        (_OPENAI_KEY_RE, "secret-like API key"),
    ]
    for pattern, label in checks:
        if pattern.search(html):
            raise ValueError(f"AI Notes HTML contains forbidden {label}")
    _validate_embedded_urls_safe(html)


def publish_html_note(
    *,
    html: str,
    title: str,
    slug: str | None = None,
    config: Mapping[str, Any] | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Publish static HTML into the configured AI Notes publish root.

    Returns a JSON-serializable dict with ok/path/url metadata. Raises
    ValueError for disabled config or unsafe publish content.
    """
    cfg = dict(config or {})
    if not cfg.get("enabled", False):
        raise ValueError("AI Notes publishing is disabled")

    _validate_html_safe_to_publish(html)
    visibility = str(cfg.get("visibility") or "public_static_html")
    allow_local = visibility.startswith("local_only")
    public_base_url = _validate_public_base_url(
        str(cfg.get("public_base_url") or ""),
        allow_local=allow_local,
    )
    root = _configured_root(cfg.get("publish_root"))

    day = today or date.today()
    day_part = day.isoformat()
    safe_slug = sanitize_slug(slug or title)
    out_dir = _join_under_root(root, [day_part])
    out_path = _join_under_root(root, [day_part, f"{safe_slug}.html"])
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")

    return {
        "ok": True,
        "title": title,
        "slug": safe_slug,
        "path": str(out_path),
        "url": f"{public_base_url}/{day_part}/{safe_slug}.html",
        "visibility": visibility,
    }
