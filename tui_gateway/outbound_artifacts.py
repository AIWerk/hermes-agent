"""Customer UI outbound artifact materialization and path redaction.

Bodies are rebound onto server.py's globals at install time (see method_ctx.bind_module),
so tests and runtime patch the facade names while this file owns the implementation.
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import hashlib
import mimetypes
import os
import re
import shutil
import stat
import sys
import tempfile
import urllib.parse
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .method_ctx import bind_module

if TYPE_CHECKING:
    from tui_gateway.server import _hermes_home, _load_cfg


_IMAGE_ATTACHMENT_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})
_AUDIO_ATTACHMENT_EXTENSIONS = frozenset({".mp3", ".m4a", ".wav", ".webm", ".ogg", ".aac", ".flac"})
_VIDEO_ATTACHMENT_EXTENSIONS = frozenset({".mp4", ".mov", ".webm", ".mkv"})
_TEXT_ATTACHMENT_EXTENSIONS = frozenset({".txt", ".md", ".csv", ".yaml", ".yml"})
_ACTIVE_ATTACHMENT_EXTENSIONS = frozenset({
    ".html", ".htm", ".xhtml", ".xht", ".xhtm", ".shtml", ".svg", ".svgz",
    ".xml", ".xsl", ".xslt", ".js", ".mjs", ".cjs", ".mhtml", ".mht", ".htc",
})
_OUTBOUND_ATTACHMENT_EXTENSIONS = (
    _IMAGE_ATTACHMENT_EXTENSIONS
    | frozenset({".pdf", ".docx", ".xlsx", ".xls", ".pptx", ".ppt", ".zip",
                 ".tar", ".gz", ".tgz", ".7z", ".rar", ".json"})
    | _TEXT_ATTACHMENT_EXTENSIONS
    | _AUDIO_ATTACHMENT_EXTENSIONS
    | _VIDEO_ATTACHMENT_EXTENSIONS
    | _ACTIVE_ATTACHMENT_EXTENSIONS
)
_OUTBOUND_ATTACHMENT_MAX_BYTES = 25 * 1024 * 1024
_OUTBOUND_ATTACHMENT_RE = re.compile(
    r"(?<![\w:/])(?:MEDIA:|file://)?(/(?:(?!\s+(?:and|or|und|oder)\s+)[^\n)\]>'\"])+?\.(?:png|jpe?g|gif|webp|pdf|txt|md|csv|json|ya?ml|docx|xlsx?|pptx?|zip|tar|gz|tgz|7z|rar|mp3|m4a|wav|webm|ogg|aac|flac|mp4|mov|mkv|html?|xhtml?|shtml|svgz?|xml|xslt?|mjs|cjs|js|mhtml?|htc))(?=$|[\s)\]>'\";,])",
    re.IGNORECASE,
)
_OUTBOUND_SAFE_URI_RE = re.compile(r"\b(?:https?://|shared://)[^\s<>'\"]+", re.IGNORECASE)
_OUTBOUND_ENCODED_SEPARATOR_TARGETS = (
    "%2f",
    "%5c",
    "%252f",
    "%255c",
)
_OUTBOUND_SAFE_URI_SCHEMES = ("http://", "https://", "shared://")
_OUTBOUND_LOCAL_PATH_RE = re.compile(
    r"(?<![\w:/])((?:(?:MEDIA:|file://)?/(?!/)[^\n)\]>'\";,]+?)"
    r"|(?:(?:MEDIA:)?[A-Za-z]:[\\/][^\n)\]>'\";,]+?)"
    r"|(?:\\\\[^\n)\]>'\";,]+?|//[^/\s]+/[^\n)\]>'\";,]+?))"
    r"(?=$|\s+(?:and|or|und|oder)\s+|[\n)\]>'\";,])",
    re.IGNORECASE,
)
_OUTBOUND_ENCODED_LOCAL_PATH_RE = re.compile(
    r"(?<![\w%])((?:%(?:25)*(?:2f|5c)){1,2}[^\s)\]>'\";,]+)", re.IGNORECASE
)
_SHARED_OUTBOUND_FOLDER_NAME = "Agent-Downloads"
_OUTBOUND_FORBIDDEN_DIR_NAMES = frozenset({
    ".ssh", ".aws", ".gnupg", ".config", ".hermes", ".secrets", ".azure", ".gcloud",
})


def _dashboard_upload_root() -> Path:
    return (Path(_hermes_home) / "dashboard_uploads").resolve()


def _attachment_preview_kind(path: Path, media_type: str) -> str:
    ext = path.suffix.lower()
    if ext in _ACTIVE_ATTACHMENT_EXTENSIONS or ext == ".json":
        return "file"
    if media_type.startswith("image/") and ext in _IMAGE_ATTACHMENT_EXTENSIONS:
        return "image"
    if media_type == "application/pdf" or ext == ".pdf":
        return "pdf"
    if media_type.startswith("audio/") or ext in _AUDIO_ATTACHMENT_EXTENSIONS:
        return "audio"
    if media_type.startswith("video/") or ext in _VIDEO_ATTACHMENT_EXTENSIONS:
        return "video"
    if media_type.startswith("text/") or ext in _TEXT_ATTACHMENT_EXTENSIONS:
        return "text"
    return "file"


def _outbound_source_roots() -> tuple[Path, ...]:
    roots: list[Path] = [_dashboard_upload_root()]
    try:
        roots.append(Path(tempfile.gettempdir()).resolve())
    except Exception:
        pass
    return tuple(roots)


def _outbound_source_allowed(path: Path) -> bool:
    try:
        resolved = path.resolve()
    except Exception:
        return False
    try:
        hermes_home = Path(_hermes_home).resolve()
        if resolved == hermes_home or hermes_home in resolved.parents:
            return False
    except Exception:
        pass
    if any(part in _OUTBOUND_FORBIDDEN_DIR_NAMES for part in resolved.parts):
        return False
    for root in _outbound_source_roots():
        try:
            if resolved == root or root in resolved.parents:
                return True
        except Exception:
            continue
    return False


def _materialize_outbound_artifact(path: Path) -> Path | None:
    if not _outbound_source_allowed(path):
        return None
    try:
        path_stat = path.stat()
        if path_stat.st_size > _OUTBOUND_ATTACHMENT_MAX_BYTES:
            return None
        digest = hashlib.sha256(f"{path}\0{path_stat.st_mtime_ns}\0{path_stat.st_size}".encode(
            "utf-8", errors="surrogatepass")).hexdigest()[:16]
        target_dir = _dashboard_upload_root() / "outbound_artifacts" / digest
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / path.name
        if not target.exists() or target.stat().st_size != path_stat.st_size:
            shutil.copy2(path, target)
            with contextlib.suppress(Exception):
                target.chmod(0o600)
        return target.resolve()
    except Exception:
        return None


def _resolve_outbound_shared_folder_root() -> Path | None:
    try:
        from hermes_cli import web_server

        root = web_server._resolve_shared_folder_root(_load_cfg())
        return root.resolve() if root else None
    except Exception:
        return None


def _shared_folder_relative_path(root: Path, path: Path) -> str | None:
    try:
        base = root.resolve()
        target = path.resolve()
        if target != base and base not in target.parents:
            return None
        parts = target.relative_to(base).parts
        if not parts or any(part in {".", ".."} or part.startswith(".") for part in parts):
            return None
        return "/".join(parts)
    except Exception:
        return None


def _file_sha256_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _shared_candidate_matches(path: Path, *, size: int, digest: str) -> bool:
    try:
        return (
            not path.is_symlink()
            and path.is_file()
            and path.stat().st_size == size
            and _file_sha256_digest(path) == digest
        )
    except Exception:
        return False


def _copy_outbound_source_to_snapshot(source: Path, snapshot: Path) -> int | None:
    def reject() -> None:
        snapshot.unlink(missing_ok=True)

    if sys.platform != "linux" or not _outbound_source_allowed(source):
        reject()
        return None
    source_absolute = Path(os.path.abspath(source))
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    file_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    for root in _outbound_source_roots():
        try:
            root_resolved = root.expanduser().resolve()
            relative = source_absolute.relative_to(root_resolved)
            if not relative.parts or any(
                part in {".", ".."} or part in _OUTBOUND_FORBIDDEN_DIR_NAMES
                for part in relative.parts
            ):
                continue
            directory_fd = os.open(root_resolved, directory_flags)
            try:
                for part in relative.parts[:-1]:
                    next_fd = os.open(part, directory_flags, dir_fd=directory_fd)
                    os.close(directory_fd)
                    directory_fd = next_fd
                source_fd = os.open(relative.parts[-1], file_flags, dir_fd=directory_fd)
            finally:
                os.close(directory_fd)
            try:
                before = os.fstat(source_fd)
                if not stat.S_ISREG(before.st_mode) or before.st_size > _OUTBOUND_ATTACHMENT_MAX_BYTES:
                    reject()
                    return None
                copied = 0
                with os.fdopen(source_fd, "rb", closefd=False) as input_handle, snapshot.open("wb") as output_handle:
                    while chunk := input_handle.read(1024 * 1024):
                        copied += len(chunk)
                        if copied > _OUTBOUND_ATTACHMENT_MAX_BYTES:
                            reject()
                            return None
                        output_handle.write(chunk)
                    output_handle.flush()
                    os.fsync(output_handle.fileno())
                after = os.fstat(source_fd)
                if (
                    (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                    != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                    or copied != before.st_size
                ):
                    reject()
                    return None
                snapshot.chmod(0o600)
                return copied
            finally:
                os.close(source_fd)
        except Exception:
            continue
    reject()
    return None


def _rename_snapshot_noreplace(snapshot: Path, target: Path) -> bool:
    if sys.platform != "linux":
        raise OSError(errno.ENOTSUP, "atomic outbound publication requires Linux")
    renameat2 = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
    if renameat2 is None:
        raise OSError(errno.ENOSYS, "renameat2 unavailable")
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    result = renameat2(-100, os.fsencode(snapshot), -100, os.fsencode(target), 1)
    if result == 0:
        return True
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        return False
    raise OSError(error_number, os.strerror(error_number), target)


def _materialize_outbound_shared_artifact(path: Path) -> tuple[Path | None, str | None, int | None]:
    root = _resolve_outbound_shared_folder_root()
    try:
        source = Path(os.path.abspath(path))
        if not _outbound_source_allowed(source):
            return None, None, None
        stem = source.stem or "file"
        suffix = source.suffix
        if not root:
            try:
                from hermes_cli import web_server

                with tempfile.TemporaryDirectory(prefix="hermes-outbound-shared-") as temp_dir:
                    snapshot = Path(temp_dir) / source.name
                    snapshot_size = _copy_outbound_source_to_snapshot(source, snapshot)
                    if snapshot_size is None:
                        return None, None, None
                    digest = _file_sha256_digest(snapshot)
                    rel = f"{_SHARED_OUTBOUND_FOLDER_NAME}/{stem}-{digest}{suffix}"
                    uploaded_rel = web_server._upload_shared_file_to_cloud(
                        web_server.load_config(), snapshot, rel)
                    return (None, uploaded_rel, snapshot_size) if uploaded_rel else (None, None, None)
            except Exception:
                return None, None, None
        rel_existing = _shared_folder_relative_path(root, source)
        source_is_agent_download = bool(rel_existing and rel_existing.split("/", 1)[0] == _SHARED_OUTBOUND_FOLDER_NAME)
        target_dir = (root / _SHARED_OUTBOUND_FOLDER_NAME).resolve()
        root_resolved = root.resolve()
        if target_dir != root_resolved and root_resolved not in target_dir.parents:
            return None, None, None
        target_dir.mkdir(parents=True, exist_ok=True)
        temp_target = target_dir / f".{source.name}.{uuid.uuid4().hex}.tmp"
        try:
            snapshot_size = _copy_outbound_source_to_snapshot(source, temp_target)
            if snapshot_size is None:
                return None, None, None
            digest = _file_sha256_digest(temp_target)
            hashed_candidates = (
                target_dir / f"{stem}-{digest[:12]}{suffix}",
                target_dir / f"{stem}-{digest}{suffix}",
            )
            candidates = hashed_candidates if source_is_agent_download else (target_dir / source.name, *hashed_candidates)
            target: Path | None = None
            for candidate in candidates:
                if _shared_candidate_matches(candidate, size=snapshot_size, digest=digest):
                    target = candidate
                    break
                if _rename_snapshot_noreplace(temp_target, candidate):
                    target = candidate
                    break
                if _shared_candidate_matches(candidate, size=snapshot_size, digest=digest):
                    target = candidate
                    break
            while target is None:
                candidate = target_dir / f"{stem}-{digest}-{uuid.uuid4().hex}{suffix}"
                if _rename_snapshot_noreplace(temp_target, candidate):
                    target = candidate
            rel = _shared_folder_relative_path(root, target)
            return target.resolve(), rel, snapshot_size
        finally:
            temp_target.unlink(missing_ok=True)
    except Exception:
        return None, None, None


def _outbound_local_path_filename(value: str) -> str:
    normalized = value.strip().replace("\\", "/")
    for prefix in ("MEDIA:", "file://"):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):]
            break
    return normalized.rstrip().rsplit("/", 1)[-1] or "file"


def _decode_outbound_path_reference(value: str) -> str:
    decoded = value
    for _ in range(3):
        expanded = urllib.parse.unquote(decoded)
        if expanded == decoded:
            break
        decoded = expanded
    return decoded


def _sanitize_outbound_text_references(text: str) -> str:
    def sanitize_unprotected(fragment: str) -> str:
        fragment = _OUTBOUND_ATTACHMENT_RE.sub(lambda match: _outbound_local_path_filename(match.group(0)), fragment)
        fragment = _OUTBOUND_LOCAL_PATH_RE.sub(lambda match: _outbound_local_path_filename(match.group(1)), fragment)
        return _OUTBOUND_ENCODED_LOCAL_PATH_RE.sub(
            lambda match: _outbound_local_path_filename(_decode_outbound_path_reference(match.group(1))),
            fragment,
        )

    pieces: list[str] = []
    cursor = 0
    for match in _OUTBOUND_SAFE_URI_RE.finditer(text):
        pieces.append(sanitize_unprotected(text[cursor:match.start()]))
        pieces.append(match.group(0))
        cursor = match.end()
    pieces.append(sanitize_unprotected(text[cursor:]))
    from agent.tool_argument_projection import sanitize_tool_display_text

    return sanitize_tool_display_text("".join(pieces))


def _outbound_encoded_prefix_suffix(text: str) -> str:
    percent = text.rfind("%")
    if percent < 0:
        return ""
    suffix = text[percent:]
    lowered = suffix.lower()
    if any(target.startswith(lowered) for target in _OUTBOUND_ENCODED_SEPARATOR_TARGETS):
        return suffix
    return ""


def _outbound_has_partial_safe_uri_scheme(text: str) -> bool:
    lowered = text.lower()
    for scheme in _OUTBOUND_SAFE_URI_SCHEMES:
        for length in range(1, len(scheme) + 1):
            if lowered.endswith(scheme[:length]):
                start = len(text) - length
                if start == 0 or not (text[start - 1].isalnum() or text[start - 1] == "_"):
                    return True
    return False


def _outbound_fragment_has_path_risk(text: str) -> bool:
    cursor = 0
    fragments: list[str] = []
    for match in _OUTBOUND_SAFE_URI_RE.finditer(text):
        fragments.append(text[cursor : match.start()])
        cursor = match.end()
    fragments.append(text[cursor:])
    risk_re = re.compile(
        r"(?i)(?<![\w:/])(?:"
        r"(?:MEDIA:|file://)?/(?:$|[A-Za-z_.~])"
        r"|\\(?:$|\\|[A-Za-z_.~])"
        r"|[A-Za-z]:[\\/]"
        r"|%(?:25)*(?:2f|5c)"
        r")"
    )
    return any(risk_re.search(fragment) or _outbound_encoded_prefix_suffix(fragment) for fragment in fragments)


def _project_outbound_stream_fragment(
    state: tuple[str, bool], incoming: str
) -> tuple[str, tuple[str, bool]]:
    buffer, in_safe_uri = state
    if in_safe_uri:
        candidate = f"{buffer}{incoming}"
        delimiter = re.search(r"[\s<>'\"]", candidate)
        if delimiter is None:
            return "", (candidate, True)
        boundary = delimiter.start()
        from agent.tool_argument_projection import sanitize_tool_display_text

        preserved = sanitize_tool_display_text(candidate[:boundary])
        projected, next_state = _project_outbound_stream_fragment(("", False), candidate[boundary:])
        return f"{preserved}{projected}", next_state

    candidate = f"{buffer}{incoming}"
    projected = ""
    while "\n" in candidate:
        line, candidate = candidate.split("\n", 1)
        projected += _sanitize_outbound_text_references(f"{line}\n")
    safe_uri_matches = list(_OUTBOUND_SAFE_URI_RE.finditer(candidate))
    if safe_uri_matches and safe_uri_matches[-1].end() == len(candidate):
        match = safe_uri_matches[-1]
        projected += _sanitize_outbound_text_references(candidate[: match.start()])
        return projected, (match.group(0), True)
    if _outbound_has_partial_safe_uri_scheme(candidate):
        return projected, (candidate, False)
    if len(candidate) > 65536:
        carry = _outbound_encoded_prefix_suffix(candidate)
        head = candidate[: -len(carry)] if carry else candidate
        return f"{projected}{_sanitize_outbound_text_references(head)}", (carry, False)
    if candidate and _outbound_fragment_has_path_risk(candidate):
        return projected, (candidate, False)
    return f"{projected}{_sanitize_outbound_text_references(candidate)}", ("", False)


def _outbound_image_attachment_payloads(text: Any) -> list[dict[str, Any]]:
    return _outbound_attachment_payloads_and_text(text)[0]


def _outbound_attachment_payloads_and_text(
    text: Any, *, append_shared_links: bool = False
) -> tuple[list[dict[str, Any]], str]:
    if not isinstance(text, str) or not text:
        return [], "" if text is None else str(text)
    attachments: list[dict[str, Any]] = []
    appended_links: list[str] = []
    delivery_errors: list[str] = []
    safe_uri_spans = [match.span() for match in _OUTBOUND_SAFE_URI_RE.finditer(text)]
    seen: set[str] = set()
    for match in _OUTBOUND_ATTACHMENT_RE.finditer(text):
        if any(start <= match.start() < end for start, end in safe_uri_spans):
            continue
        raw_path = match.group(1)
        try:
            source_path = Path(raw_path).expanduser().resolve()
        except Exception:
            continue
        if (
            str(source_path) in seen
            or not source_path.is_file()
            or source_path.suffix.lower() not in _OUTBOUND_ATTACHMENT_EXTENSIONS
        ):
            continue
        seen.add(str(source_path))
        if not _outbound_source_allowed(source_path):
            continue
        try:
            source_size = source_path.stat().st_size
        except Exception:
            continue
        if source_size > _OUTBOUND_ATTACHMENT_MAX_BYTES:
            continue
        media_type = mimetypes.guess_type(source_path.name)[0] or "application/octet-stream"
        preview_kind = _attachment_preview_kind(source_path, media_type)
        safe_renderable = preview_kind in {"image", "pdf", "text", "audio", "video"}
        _shared_path, shared_rel_path, shared_size = _materialize_outbound_shared_artifact(source_path)
        if not shared_rel_path:
            delivery_errors.append(
                f"Die Datei {source_path.name} konnte nicht unter Agent-Downloads bereitgestellt werden.")
            continue
        payload_path = f"shared://{urllib.parse.quote(shared_rel_path, safe='/')}"
        shared_open_url = (
            "/api/assistant/shared-folder/open?path="
            f"{urllib.parse.quote(shared_rel_path, safe='')}"
        )
        open_url = shared_open_url
        download_url = shared_open_url
        public_link: dict[str, str] | None = None
        if append_shared_links:
            try:
                from hermes_cli import web_server

                public_link = web_server._create_shared_file_public_link(
                    web_server.load_config(), shared_rel_path, name=source_path.stem or source_path.name)
            except Exception:
                public_link = None
            if public_link:
                open_url = public_link.get("url") or shared_open_url
                download_url = public_link.get("download_url") or open_url
            if shared_open_url not in text:
                line = f"{source_path.name}: {shared_open_url}"
                if public_link:
                    line = (
                        f"{source_path.name}: {shared_open_url}\n"
                        f"  Web-Link: {public_link.get('url')}\n"
                        f"  Download: {public_link.get('download_url')}"
                    )
                appended_links.append(line)
        preview_url = open_url if safe_renderable else None
        payload = {
            "name": source_path.name,
            "kind": "file",
            "path": payload_path,
            "type": media_type,
            "size": shared_size if shared_size is not None else source_size,
            "is_image": preview_kind == "image",
            "open_url": open_url,
            "download_url": download_url,
            "preview_url": preview_url,
            "preview_kind": preview_kind,
            "safe_renderable": safe_renderable,
            "shared_folder_path": shared_rel_path,
        }
        if public_link:
            payload["public_url"] = public_link.get("url")
            payload["public_download_url"] = public_link.get("download_url")
        attachments.append(payload)
    text = _sanitize_outbound_text_references(text)
    if append_shared_links and appended_links:
        suffix = "\n".join(f"- {line}" for line in appended_links)
        text = f"{text.rstrip()}\n\nIm Shared-Ordner unter Agent-Downloads abgelegt:\n{suffix}"
    if delivery_errors:
        text = f"{text.rstrip()}\n\n" + "\n".join(delivery_errors)
    return attachments, text


def register(server) -> None:
    bind_module(globals(), server, skip=("_",))
