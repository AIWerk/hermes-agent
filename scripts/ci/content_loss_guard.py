#!/usr/bin/env python3
"""Fail-closed AIWerk fork content-loss guard.

The guard reads authority only from the exact protected base commit. Candidate
content is inspected as Git objects and is never imported or executed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

BASELINE_PATH = ".ci/content-loss/baseline.json"
RETIREMENTS_PATH = ".ci/content-loss/retirements.json"
_HISTORICAL_ACTIVE = "8bd56009c186b6e0fddc8aa96898201a42a1efb4"
_HISTORICAL_TARGET = "7b0cf741a009bc3c61b44f9cefef7815aab88da3"
_HISTORICAL_UPSTREAM = "8f2f4caff42cfce29d6fc0992b9f4157409f8d03"
_OID_RE = re.compile(r"^[0-9a-f]{40}$")
_REGULAR_MODES = {"100644", "100755"}
_GUARD_VERSION = "r7a-v1"
_RECOVERY_PACKET_SHA256 = "8262d0e5fac12fc0b6f856f0e7e7091fe9e089e78310aec7f16dc2d120a089ec"
_RECOVERY_LEDGER_SHA256 = "b99ea555b9ee0eeac3e60ce702502b217f5aa130e89ec2c8cecb0d24b8dba894"


class ContentLossError(RuntimeError):
    """Evidence, authority, or object-graph error that must fail closed."""


def _run_git(repo: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repo,
            input=input_bytes,
            capture_output=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode("utf-8", "replace").strip()
        raise ContentLossError(f"git {' '.join(args)} failed: {detail}") from exc
    return result.stdout


def _resolve(repo: Path, ref: str, kind: str) -> str:
    suffix = "^{commit}" if kind == "commit" else "^{tree}"
    oid = _run_git(repo, "rev-parse", "--verify", f"{ref}{suffix}").decode().strip()
    if not _OID_RE.fullmatch(oid):
        raise ContentLossError(f"invalid {kind} object id for {ref!r}: {oid!r}")
    actual = _run_git(repo, "cat-file", "-t", oid).decode().strip()
    if actual != kind:
        raise ContentLossError(f"expected {kind} for {ref!r}, found {actual!r}")
    return oid


def _commit_if_present(repo: Path, ref: str) -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", f"{ref}^{{commit}}"],
        cwd=repo,
        capture_output=True,
    )
    if result.returncode != 0:
        return None
    oid = result.stdout.decode("ascii", "strict").strip()
    return oid if _OID_RE.fullmatch(oid) else None


def _commit_parents(repo: Path, ref: str) -> list[str]:
    commit = _resolve(repo, ref, "commit")
    raw = _run_git(repo, "show", "-s", "--format=%P", commit).decode("ascii").strip()
    parents = raw.split() if raw else []
    if not all(_OID_RE.fullmatch(parent) for parent in parents):
        raise ContentLossError(f"invalid parent list for {commit}")
    return parents


def _tree_entries(repo: Path, ref: str) -> dict[str, dict[str, str]]:
    tree = _resolve(repo, ref, "tree")
    raw = _run_git(repo, "ls-tree", "-rz", "--full-tree", "-r", tree)
    entries: dict[str, dict[str, str]] = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        try:
            header, raw_path = record.split(b"\t", 1)
            mode, obj_type, oid = header.decode("ascii").split(" ", 2)
            path = raw_path.decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise ContentLossError("invalid or non-UTF-8 Git tree entry") from exc
        if path in entries:
            raise ContentLossError(f"duplicate Git tree path: {path}")
        entries[path] = {"mode": mode, "type": obj_type, "oid": oid}
    return entries


def _read_blob(repo: Path, oid: str) -> bytes:
    payload = _run_git(repo, "cat-file", "blob", oid)
    actual = hashlib.sha1(b"blob " + str(len(payload)).encode("ascii") + b"\0" + payload).hexdigest()
    if actual != oid:
        raise ContentLossError(f"Git blob payload hash mismatch: expected {oid}, got {actual}")
    return payload


def _read_blobs(repo: Path, oids: Iterable[str]) -> dict[str, bytes]:
    unique = list(dict.fromkeys(oids))
    if not unique:
        return {}
    proc = subprocess.Popen(
        ["git", "cat-file", "--batch"],
        cwd=repo,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
    payloads: dict[str, bytes] = {}
    try:
        for requested in unique:
            proc.stdin.write(requested.encode("ascii") + b"\n")
            proc.stdin.flush()
            header = proc.stdout.readline()
            parts = header.rstrip(b"\n").split(b" ")
            if len(parts) != 3 or parts[1] != b"blob":
                raise ContentLossError(f"missing or non-blob Git object: {requested}")
            resolved = parts[0].decode("ascii")
            size = int(parts[2])
            payload = proc.stdout.read(size)
            if len(payload) != size or proc.stdout.read(1) != b"\n":
                raise ContentLossError(f"truncated Git blob payload: {requested}")
            actual = hashlib.sha1(
                b"blob " + str(len(payload)).encode("ascii") + b"\0" + payload
            ).hexdigest()
            if resolved != requested or actual != requested:
                raise ContentLossError(f"Git blob payload hash mismatch: expected {requested}, got {actual}")
            payloads[requested] = payload
    finally:
        proc.stdin.close()
    stderr = proc.stderr.read().decode("utf-8", "replace").strip()
    rc = proc.wait()
    if rc != 0:
        raise ContentLossError(f"git cat-file --batch failed: {stderr}")
    return payloads


def _strict_json(raw: bytes, label: str) -> Any:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContentLossError(f"{label} is not UTF-8") from exc

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ContentLossError(f"{label}: duplicate JSON key {key!r}")
            result[key] = value
        return result

    try:
        return json.loads(text, object_pairs_hook=pairs)
    except json.JSONDecodeError as exc:
        raise ContentLossError(f"{label} is invalid JSON: {exc}") from exc


def _blob_at(repo: Path, entries: dict[str, dict[str, str]], path: str, label: str) -> tuple[str, bytes]:
    entry = entries.get(path)
    if entry is None:
        raise ContentLossError(f"{label} missing from authority tree: {path}")
    if entry["type"] != "blob" or entry["mode"] not in _REGULAR_MODES:
        raise ContentLossError(f"{label} is not a regular file: {path}")
    return entry["oid"], _read_blob(repo, entry["oid"])


def _require_exact_keys(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContentLossError(f"{label} must be an object")
    actual = set(value)
    if actual != keys:
        raise ContentLossError(
            f"{label} keys mismatch: missing={sorted(keys - actual)} extra={sorted(actual - keys)}"
        )
    return value


def _validate_baseline(value: Any) -> dict[str, Any]:
    baseline = _require_exact_keys(
        value,
        {
            "schema_version",
            "baseline_id",
            "bootstrap",
            "accepted_upstream_commit",
            "recovery_ledger",
            "markers",
            "protected_paths",
            "selectors",
            "protected_controls",
        },
        "baseline",
    )
    if baseline["schema_version"] != 1 or not isinstance(baseline["baseline_id"], str):
        raise ContentLossError("unsupported baseline schema")
    bootstrap = _require_exact_keys(
        baseline["bootstrap"], {"source_commit", "source_tree", "path_count"}, "baseline.bootstrap"
    )
    if not _OID_RE.fullmatch(str(bootstrap["source_commit"])):
        raise ContentLossError("baseline bootstrap source_commit is not a full object id")
    if not _OID_RE.fullmatch(str(bootstrap["source_tree"])):
        raise ContentLossError("baseline bootstrap source_tree is not a full object id")
    if not isinstance(bootstrap["path_count"], int) or bootstrap["path_count"] < 0:
        raise ContentLossError("baseline bootstrap path_count must be a non-negative integer")
    if not _OID_RE.fullmatch(str(baseline["accepted_upstream_commit"])):
        raise ContentLossError("accepted_upstream_commit is not a full object id")
    ledger = _require_exact_keys(
        baseline["recovery_ledger"],
        {
            "status",
            "canonical_recovery_complete",
            "source_packet_sha256",
            "canonical_ledger_sha256",
            "total",
            "deleted",
            "marker_zeroed",
            "entries",
        },
        "recovery ledger",
    )
    if ledger["status"] != "incomplete-bootstrap" or ledger["canonical_recovery_complete"] is not False:
        raise ContentLossError("R7A recovery ledger must remain incomplete-bootstrap")
    for field in ("source_packet_sha256", "canonical_ledger_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", str(ledger[field])):
            raise ContentLossError(f"recovery ledger {field} must be a SHA-256")
    entries = ledger["entries"]
    if not isinstance(entries, list):
        raise ContentLossError("recovery ledger entries must be a list")
    seen_paths: set[str] = set()
    deleted = 0
    marker_zeroed = 0
    for item in entries:
        entry = _require_exact_keys(item, {"incident_kind", "path"}, "recovery ledger entry")
        path = entry["path"]
        kind = entry["incident_kind"]
        if not isinstance(path, str) or not path or path in seen_paths:
            raise ContentLossError("recovery ledger paths must be unique non-empty strings")
        if kind == "deleted":
            deleted += 1
        elif kind == "aiwerk_content_removed":
            marker_zeroed += 1
        else:
            raise ContentLossError(f"unsupported recovery incident kind: {kind!r}")
        seen_paths.add(path)
    if any(type(ledger[field]) is not int or ledger[field] < 0 for field in ("total", "deleted", "marker_zeroed")):
        raise ContentLossError("recovery ledger counts must be non-negative integers")
    if (ledger["total"], ledger["deleted"], ledger["marker_zeroed"]) != (
        len(entries),
        deleted,
        marker_zeroed,
    ):
        raise ContentLossError("recovery ledger count mismatch")
    canonical_entries = (
        json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    if hashlib.sha256(canonical_entries).hexdigest() != ledger["canonical_ledger_sha256"]:
        raise ContentLossError("recovery ledger digest mismatch")
    markers = baseline["markers"]
    if not isinstance(markers, list) or not all(isinstance(item, dict) for item in markers):
        raise ContentLossError("baseline markers must be objects")
    protected_paths = baseline["protected_paths"]
    if not isinstance(protected_paths, list) or not all(
        isinstance(item, dict) for item in protected_paths
    ):
        raise ContentLossError("baseline protected_paths must be objects")
    protected_ids: set[str] = set()
    protected_names: set[str] = set()
    for item in protected_paths:
        item_keys = set(item)
        base_keys = {"id", "path", "required"}
        if item_keys not in {frozenset(base_keys), frozenset(base_keys | {"markers"})}:
            raise ContentLossError(
                "protected path keys mismatch: expected id/path/required with optional markers"
            )
        if not isinstance(item["id"], str) or not item["id"]:
            raise ContentLossError("protected path id must be a non-empty string")
        if not isinstance(item["path"], str) or not item["path"]:
            raise ContentLossError("protected path path must be a non-empty string")
        if item["required"] is not True:
            raise ContentLossError("protected path required must be true")
        item_markers = item.get("markers", [])
        if not isinstance(item_markers, list) or not all(
            isinstance(marker, str) and marker for marker in item_markers
        ):
            raise ContentLossError("protected path markers must be non-empty strings")
        if item["id"] in protected_ids or item["path"] in protected_names:
            raise ContentLossError("protected path records must be unique")
        protected_ids.add(item["id"])
        protected_names.add(item["path"])
    selectors = baseline["selectors"]
    if not isinstance(selectors, list) or not all(isinstance(item, dict) for item in selectors):
        raise ContentLossError("baseline selectors must be objects")
    controls = baseline["protected_controls"]
    if not isinstance(controls, list) or not controls or not all(isinstance(p, str) and p for p in controls):
        raise ContentLossError("baseline protected_controls must be a non-empty path list")
    if len(controls) != len(set(controls)):
        raise ContentLossError("baseline protected_controls contains duplicates")
    return baseline


def _validate_retirements(value: Any) -> dict[str, Any]:
    retirements = _require_exact_keys(value, {"schema_version", "approvals"}, "retirements")
    if retirements["schema_version"] != 1 or not isinstance(retirements["approvals"], list):
        raise ContentLossError("unsupported retirements schema")
    ids: set[str] = set()
    required = {
        "id",
        "subject_path",
        "expected_old_blob",
        "allowed_target_state",
        "valid_for_pr",
        "not_before",
        "expires_at",
        "reason",
    }
    for index, raw in enumerate(retirements["approvals"]):
        item = _require_exact_keys(raw, required, f"retirements.approvals[{index}]")
        if not isinstance(item["id"], str) or not item["id"] or item["id"] in ids:
            raise ContentLossError("retirement ids must be unique non-empty strings")
        ids.add(item["id"])
        if not isinstance(item["subject_path"], str) or not item["subject_path"]:
            raise ContentLossError(f"retirement {item['id']} has invalid subject_path")
        if not _OID_RE.fullmatch(str(item["expected_old_blob"])):
            raise ContentLossError(f"retirement {item['id']} has invalid expected_old_blob")
        target_state = item["allowed_target_state"]
        if target_state != "absent" and not (
            isinstance(target_state, str)
            and target_state.startswith("blob:")
            and _OID_RE.fullmatch(target_state[5:])
        ):
            raise ContentLossError(f"retirement {item['id']} has unsupported target state")
        if not isinstance(item["valid_for_pr"], int) or item["valid_for_pr"] <= 0:
            raise ContentLossError(f"retirement {item['id']} has invalid PR binding")
        if not isinstance(item["reason"], str) or not item["reason"].strip():
            raise ContentLossError(f"retirement {item['id']} has no reason")
        parsed_times: dict[str, datetime] = {}
        for field in ("not_before", "expires_at"):
            try:
                parsed = datetime.fromisoformat(str(item[field]).replace("Z", "+00:00"))
            except ValueError as exc:
                raise ContentLossError(f"retirement {item['id']} has invalid {field}") from exc
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ContentLossError(
                    f"retirement {item['id']} {field} must include timezone"
                )
            parsed_times[field] = parsed.astimezone(timezone.utc)
        if parsed_times["not_before"] >= parsed_times["expires_at"]:
            raise ContentLossError(
                f"retirement {item['id']} has non-positive validity interval"
            )
    return retirements


def _historical_baseline() -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    baseline = {
        "schema_version": 1,
        "baseline_id": "historical-8bd56009",
        "bootstrap": {
            "source_commit": _HISTORICAL_ACTIVE,
            "source_tree": "635bd1e2b6762b804e6c6e3b9fcfdb768135fbf4",
            "path_count": 0,
        },
        "accepted_upstream_commit": "8f2f4caff42cfce29d6fc0992b9f4157409f8d03",
        "recovery_ledger": {
            "status": "incomplete-bootstrap",
            "canonical_recovery_complete": False,
            "source_packet_sha256": "8262d0e5fac12fc0b6f856f0e7e7091fe9e089e78310aec7f16dc2d120a089ec",
            "canonical_ledger_sha256": "37517e5f3dc66819f61f5a7bb8ace1921282415f10551d2defa5c3eb0985b570",
            "total": 0,
            "deleted": 0,
            "marker_zeroed": 0,
            "entries": [],
        },
        "markers": [
            {
                "id": "aiwerk-brand",
                "needle": "aiwerk",
                "normalization": "unicode-nfkc-casefold",
                "zero_transition": "block",
                "decrease": "report",
            }
        ],
        "protected_paths": [
            {"id": "immutable-release-contract", "path": "immutable-release-build.json", "required": True},
            {"id": "aiwerk-cui-page", "path": "web/src/pages/AiwerkAssistantPage.tsx", "required": True},
            {"id": "aiwerk-cui-slash", "path": "web/src/lib/cui-slash.ts", "required": True},
        ],
        "selectors": [],
        "protected_controls": [BASELINE_PATH, RETIREMENTS_PATH],
    }
    return (
        baseline,
        {"schema_version": 1, "approvals": []},
        {"baseline": "historical-built-in", "retirements": "historical-built-in"},
    )


def _load_authority(
    repo: Path, active_sha: str, active_entries: dict[str, dict[str, str]]
) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    if BASELINE_PATH not in active_entries:
        if active_sha == _HISTORICAL_ACTIVE:
            return _historical_baseline()
        raise ContentLossError(f"base authority missing: {BASELINE_PATH}")
    baseline_oid, baseline_raw = _blob_at(repo, active_entries, BASELINE_PATH, "baseline")
    retirements_oid, retirements_raw = _blob_at(
        repo, active_entries, RETIREMENTS_PATH, "retirements"
    )
    baseline = _validate_baseline(_strict_json(baseline_raw, "baseline"))
    retirements = _validate_retirements(_strict_json(retirements_raw, "retirements"))
    return baseline, retirements, {
        "baseline": baseline_oid,
        "retirements": retirements_oid,
    }


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ContentLossError("retirement timestamp must include timezone")
    return parsed.astimezone(timezone.utc)


def _matching_retirement(
    approvals: list[dict[str, Any]],
    *,
    path: str,
    old_blob: str,
    pr_number: int | None,
    now: datetime,
) -> dict[str, Any] | None:
    if pr_number is None:
        return None
    matches = [
        item
        for item in approvals
        if item["subject_path"] == path
        and item["expected_old_blob"] == old_blob
        and item["allowed_target_state"] == "absent"
        and item["valid_for_pr"] == pr_number
        and _parse_time(item["not_before"]) <= now.astimezone(timezone.utc) < _parse_time(item["expires_at"])
    ]
    if len(matches) > 1:
        raise ContentLossError(f"multiple retirements match {path}")
    return matches[0] if matches else None


def _matching_control_transition(
    approvals: list[dict[str, Any]],
    *,
    path: str,
    old_blob: str,
    target_blob: str,
    pr_number: int | None,
    now: datetime,
) -> dict[str, Any] | None:
    if pr_number is None:
        return None
    matches = [
        item
        for item in approvals
        if item["subject_path"] == path
        and item["expected_old_blob"] == old_blob
        and item["allowed_target_state"] == f"blob:{target_blob}"
        and item["valid_for_pr"] == pr_number
        and _parse_time(item["not_before"])
        <= now.astimezone(timezone.utc)
        < _parse_time(item["expires_at"])
    ]
    if len(matches) > 1:
        raise ContentLossError(f"multiple control transitions match {path}")
    return matches[0] if matches else None


def _validate_append_only_approvals(
    *,
    repo: Path,
    active_entries: dict[str, dict[str, str]],
    target_entries: dict[str, dict[str, str]],
    base_retirements: dict[str, Any],
    controls: set[str],
    pr_number: int | None,
) -> dict[str, list[str]] | None:
    target_entry = target_entries.get(RETIREMENTS_PATH)
    if target_entry is None or target_entry["type"] != "blob" or target_entry["mode"] not in _REGULAR_MODES:
        return None
    candidate = _validate_retirements(
        _strict_json(_read_blob(repo, target_entry["oid"]), "candidate retirements")
    )
    old = base_retirements["approvals"]
    new = candidate["approvals"]
    if len(new) <= len(old) or new[: len(old)] != old or pr_number is None:
        return None
    added = new[len(old) :]
    control_ids: list[str] = []
    retirement_ids: list[str] = []
    for item in added:
        subject = item["subject_path"]
        active = active_entries.get(subject)
        state = item["allowed_target_state"]
        if (
            active is None
            or active["type"] != "blob"
            or active["mode"] not in _REGULAR_MODES
            or item["expected_old_blob"] != active["oid"]
            or item["valid_for_pr"] <= pr_number
        ):
            return None
        if state == "absent":
            if subject in controls:
                return None
            retirement_ids.append(item["id"])
        elif state.startswith("blob:"):
            if subject not in controls or subject == RETIREMENTS_PATH:
                return None
            control_ids.append(item["id"])
        else:
            return None
    return {
        "control": sorted(control_ids),
        "path_retirement": sorted(retirement_ids),
    }


def _json_pointer(value: Any, pointer: str) -> Any:
    if pointer == "":
        return value
    if not pointer.startswith("/"):
        raise ContentLossError(f"invalid JSON pointer: {pointer}")
    current = value
    for raw_part in pointer[1:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            raise KeyError(pointer)
    return current


def _evaluate_selectors(
    repo: Path,
    target_entries: dict[str, dict[str, str]],
    selectors: list[dict[str, Any]],
) -> tuple[dict[str, str], list[str]]:
    statuses: dict[str, str] = {}
    reasons: list[str] = []
    parsed_json: dict[str, Any] = {}
    for selector in selectors:
        selector_id = selector.get("id")
        selector_type = selector.get("type")
        path = selector.get("path")
        if not isinstance(selector_id, str) or not isinstance(path, str):
            raise ContentLossError("selector id and path must be strings")
        if selector_id in statuses:
            raise ContentLossError(f"duplicate selector id: {selector_id}")
        entry = target_entries.get(path)
        passed = entry is not None and entry["type"] == "blob" and entry["mode"] in _REGULAR_MODES
        if passed and selector_type == "path_exists":
            pass
        elif passed and selector_type in {"json_pointer_equals", "json_array_contains_object"}:
            if path not in parsed_json:
                parsed_json[path] = _strict_json(_read_blob(repo, entry["oid"]), f"selector file {path}")
            try:
                selected = _json_pointer(parsed_json[path], selector["pointer"])
            except (KeyError, TypeError):
                passed = False
            else:
                if selector_type == "json_pointer_equals":
                    passed = selected == selector.get("value")
                else:
                    where = selector.get("where")
                    required_keys = selector.get("required_keys")
                    if not isinstance(selected, list) or not isinstance(where, dict) or not isinstance(required_keys, list):
                        raise ContentLossError(f"invalid array selector: {selector_id}")
                    passed = any(
                        isinstance(item, dict)
                        and all(item.get(key) == value for key, value in where.items())
                        and all(key in item for key in required_keys)
                        for item in selected
                    )
        elif selector_type not in {"path_exists", "json_pointer_equals", "json_array_contains_object"}:
            raise ContentLossError(f"unsupported selector type: {selector_type}")
        statuses[selector_id] = "passed" if passed else "failed"
        if not passed:
            reasons.append("CAPABILITY_SELECTOR_FAILED")
    return statuses, reasons


def _marker_measurements(
    repo: Path,
    active_entries: dict[str, dict[str, str]],
    target_entries: dict[str, dict[str, str]],
    markers: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    common_regular = [
        path
        for path in active_entries.keys() & target_entries.keys()
        if active_entries[path]["type"] == "blob"
        and active_entries[path]["mode"] in _REGULAR_MODES
    ]
    active_payloads = _read_blobs(repo, (active_entries[path]["oid"] for path in common_regular))
    zeroes: list[dict[str, Any]] = []
    decreases: list[dict[str, Any]] = []
    reasons: list[str] = []
    for marker in markers:
        expected_keys = {"id", "needle", "normalization", "zero_transition", "decrease"}
        _require_exact_keys(marker, expected_keys, "marker")
        if marker["normalization"] != "unicode-nfkc-casefold" or marker["zero_transition"] != "block":
            raise ContentLossError(f"unsupported marker policy: {marker.get('id')}")
        needle = unicodedata.normalize("NFKC", str(marker["needle"])).casefold()
        if not needle:
            raise ContentLossError("marker needle must be non-empty")
        candidates: list[tuple[str, int]] = []
        for path in common_regular:
            old_payload = active_payloads[active_entries[path]["oid"]]
            try:
                old_text = old_payload.decode("utf-8")
            except UnicodeDecodeError:
                continue
            old_count = unicodedata.normalize("NFKC", old_text).casefold().count(needle)
            if old_count:
                candidates.append((path, old_count))
        target_payloads = _read_blobs(repo, (target_entries[path]["oid"] for path, _ in candidates))
        for path, old_count in candidates:
            payload = target_payloads[target_entries[path]["oid"]]
            try:
                new_text = payload.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ContentLossError(f"marker-bearing text became non-UTF-8: {path}") from exc
            new_count = unicodedata.normalize("NFKC", new_text).casefold().count(needle)
            finding = {
                "marker_id": marker["id"],
                "new_count": new_count,
                "old_count": old_count,
                "path": path,
            }
            if new_count == 0:
                zeroes.append(finding)
                reasons.append("FORK_MARKER_ZEROED")
            elif new_count < old_count:
                decreases.append(finding)
    zeroes.sort(key=lambda item: (item["path"], item["marker_id"]))
    decreases.sort(key=lambda item: (item["path"], item["marker_id"]))
    return zeroes, decreases, reasons


def make_effective_merge_tree(repo: Path, base_ref: str, head_ref: str) -> str:
    base = _resolve(repo, base_ref, "commit")
    head = _resolve(repo, head_ref, "commit")
    raw = _run_git(repo, "merge-tree", "--write-tree", base, head).decode("utf-8", "strict")
    first = raw.splitlines()[0].strip() if raw.splitlines() else ""
    if not _OID_RE.fullmatch(first):
        raise ContentLossError("git merge-tree did not produce an exact tree id")
    _resolve(repo, first, "tree")
    return first


def evaluate_range(
    repo: Path | str,
    active_ref: str,
    target_ref: str,
    upstream_ref: str,
    *,
    pr_number: int | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    root = Path(repo).resolve()
    now = now or datetime.now(timezone.utc)
    active_sha = _resolve(root, active_ref, "commit")
    target_tree = _resolve(root, target_ref, "tree")
    upstream_sha = _resolve(root, upstream_ref, "commit")
    active_entries = _tree_entries(root, active_sha)
    target_entries = _tree_entries(root, target_tree)
    upstream_entries = _tree_entries(root, upstream_sha)
    baseline, retirements, authority_blobs = _load_authority(root, active_sha, active_entries)
    accepted_upstream = _resolve(root, baseline["accepted_upstream_commit"], "commit")
    if accepted_upstream != upstream_sha:
        raise ContentLossError(
            f"upstream authority mismatch: baseline={accepted_upstream} argument={upstream_sha}"
        )

    reasons: list[str] = []
    applied: list[str] = []
    protected = {
        item["path"]: item
        for item in baseline["protected_paths"]
        if isinstance(item.get("path"), str) and item.get("required") is True
    }
    protected_marker_losses: list[dict[str, Any]] = []
    for path, item in sorted(protected.items()):
        required_markers = item.get("markers", [])
        target_entry = target_entries.get(path)
        if not required_markers or target_entry is None or target_entry.get("type") != "blob":
            continue
        target_text = _read_blob(root, target_entry["oid"]).decode("utf-8", "replace")
        missing_markers = [marker for marker in required_markers if marker not in target_text]
        if missing_markers:
            reasons.append("PROTECTED_PATH_MARKER_REMOVED")
            protected_marker_losses.append(
                {"path": path, "missing_markers": missing_markers}
            )
    missing_items: list[dict[str, Any]] = []
    for path in sorted(active_entries.keys() - target_entries.keys()):
        old = active_entries[path]
        retirement = _matching_retirement(
            retirements["approvals"],
            path=path,
            old_blob=old["oid"],
            pr_number=pr_number,
            now=now,
        )
        upstream_entry = upstream_entries.get(path)
        if upstream_entry is None:
            classification = "fork-owned"
        elif old != upstream_entry:
            classification = "fork-modified"
        else:
            classification = "upstream-origin"
        verdict = "reported"
        if retirement is not None:
            verdict = "approved-retirement"
            applied.append(retirement["id"])
        elif path in protected:
            verdict = "blocked-protected-loss"
            reasons.append("PROTECTED_PATH_REMOVED")
        elif classification != "upstream-origin":
            verdict = "blocked-fork-loss"
            reasons.append("FORK_OWNED_PATH_REMOVED")
        missing_items.append(
            {
                "classification": classification,
                "mode": old["mode"],
                "oid": old["oid"],
                "path": path,
                "type": old["type"],
                "verdict": verdict,
            }
        )

    mode_type_transitions: list[dict[str, str]] = []
    for path in sorted(active_entries.keys() & target_entries.keys()):
        old = active_entries[path]
        new = target_entries[path]
        if old.get("mode") == new.get("mode") and old.get("type") == new.get("type"):
            continue
        if old == upstream_entries.get(path):
            continue
        mode_type_transitions.append(
            {
                "new_mode": new["mode"],
                "new_type": new["type"],
                "old_mode": old["mode"],
                "old_type": old["type"],
                "path": path,
            }
        )
        reasons.append("FORK_OWNED_MODE_OR_TYPE_CHANGED")

    for path in sorted(protected.keys() & target_entries.keys()):
        target_entry = target_entries[path]
        if target_entry["type"] != "blob" or target_entry["mode"] not in _REGULAR_MODES:
            reasons.append("PROTECTED_PATH_TYPE_CHANGED")

    zeroes, decreases, marker_reasons = _marker_measurements(
        root, active_entries, target_entries, baseline["markers"]
    )
    reasons.extend(marker_reasons)
    selector_statuses, selector_reasons = _evaluate_selectors(root, target_entries, baseline["selectors"])
    reasons.extend(selector_reasons)

    changed = sorted(
        path
        for path in active_entries.keys() | target_entries.keys()
        if active_entries.get(path) != target_entries.get(path)
    )
    control_set = set(baseline["protected_controls"])
    control_changes = sorted(path for path in changed if path in control_set)
    non_control_changes = [path for path in changed if path not in control_set]
    control_approvals_added: list[str] = []
    path_retirement_approvals_added: list[str] = []
    control_transitions_applied: list[str] = []
    if control_changes and non_control_changes:
        reasons.append("MIXED_CONTROL_AND_PRODUCT_CHANGE")
    elif control_changes == [RETIREMENTS_PATH]:
        added = _validate_append_only_approvals(
            repo=root,
            active_entries=active_entries,
            target_entries=target_entries,
            base_retirements=retirements,
            controls=control_set,
            pr_number=pr_number,
        )
        if added is None:
            reasons.append("UNAPPROVED_CONTROL_PLANE_CHANGE")
        else:
            control_approvals_added = added["control"]
            path_retirement_approvals_added = added["path_retirement"]
    elif control_changes:
        for path in control_changes:
            active_entry = active_entries.get(path)
            target_entry = target_entries.get(path)
            transition = None
            if (
                active_entry is not None
                and target_entry is not None
                and active_entry["type"] == "blob"
                and active_entry["mode"] in _REGULAR_MODES
                and target_entry["type"] == "blob"
                and target_entry["mode"] in _REGULAR_MODES
            ):
                transition = _matching_control_transition(
                    retirements["approvals"],
                    path=path,
                    old_blob=active_entry["oid"],
                    target_blob=target_entry["oid"],
                    pr_number=pr_number,
                    now=now,
                )
            if transition is None:
                reasons.append("UNAPPROVED_CONTROL_PLANE_CHANGE")
            else:
                control_transitions_applied.append(transition["id"])
                if path == BASELINE_PATH:
                    assert target_entry is not None
                    _validate_baseline(
                        _strict_json(
                            _read_blob(root, target_entry["oid"]),
                            "candidate baseline",
                        )
                    )

    reason_codes = sorted(set(reasons))
    report = {
        "authority_blobs": authority_blobs,
        "authority_manifest_blob": authority_blobs["baseline"],
        "authority_ref": active_sha,
        "base_tree": _resolve(root, active_sha, "tree"),
        "completeness": {
            "all_selectors_evaluated": len(selector_statuses) == len(baseline["selectors"]),
            "authority_loaded": True,
            "git_objects_complete": True,
            "reports_complete": True,
            "upstream_classification_complete": True,
        },
        "control_plane_changes": control_changes,
        "control_transition_approvals_added": control_approvals_added,
        "control_transitions_applied": sorted(control_transitions_applied),
        "path_retirement_approvals_added": path_retirement_approvals_added,
        "generated_at": now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "guard_version": _GUARD_VERSION,
        "program_status": {
            "canonical_recovery_complete": baseline["recovery_ledger"][
                "canonical_recovery_complete"
            ],
            "phase": baseline["recovery_ledger"]["status"],
            "tracked_incident_paths": baseline["recovery_ledger"]["total"],
        },
        "input_refs": {
            "active": active_sha,
            "target": _commit_if_present(root, target_ref) or target_tree,
            "upstream": upstream_sha,
        },
        "measurements": {
            "marker_decreases": decreases,
            "marker_zero_transitions": zeroes,
            "missing_paths": missing_items,
            "mode_type_transitions": mode_type_transitions,
            "protected_marker_losses": protected_marker_losses,
        },
        "mode": "range",
        "pr_number": pr_number,
        "reason_codes": reason_codes,
        "retirements_applied": sorted(applied),
        "schema_version": 1,
        "selectors": dict(sorted(selector_statuses.items())),
        "target_tree": target_tree,
        "upstream_sha": upstream_sha,
        "verdict": "FAIL" if reason_codes else "PASS",
    }
    return report


def validate_historical_recovery_bijection(
    report: dict[str, Any], recovery_ledger: dict[str, Any]
) -> None:
    if report.get("verdict") != "FAIL":
        raise ContentLossError("historical fixture did not produce FAIL")
    if report.get("input_refs") != {
        "active": _HISTORICAL_ACTIVE,
        "target": _HISTORICAL_TARGET,
        "upstream": _HISTORICAL_UPSTREAM,
    }:
        raise ContentLossError("historical fixture identity mismatch")
    if (
        recovery_ledger.get("status") != "incomplete-bootstrap"
        or recovery_ledger.get("canonical_recovery_complete") is not False
        or recovery_ledger.get("source_packet_sha256") != _RECOVERY_PACKET_SHA256
        or recovery_ledger.get("canonical_ledger_sha256") != _RECOVERY_LEDGER_SHA256
        or recovery_ledger.get("total") != 135
        or recovery_ledger.get("deleted") != 100
        or recovery_ledger.get("marker_zeroed") != 35
    ):
        raise ContentLossError("historical recovery ledger identity mismatch")
    entries = recovery_ledger.get("entries")
    if not isinstance(entries, list):
        raise ContentLossError("historical recovery ledger entries missing")
    ledger_deleted = {
        item["path"] for item in entries if item.get("incident_kind") == "deleted"
    }
    ledger_zeroed = {
        item["path"]
        for item in entries
        if item.get("incident_kind") == "aiwerk_content_removed"
    }
    missing = {
        item["path"] for item in report["measurements"]["missing_paths"]
    }
    zeroed = {
        item["path"]
        for item in report["measurements"]["marker_zero_transitions"]
    }
    if (
        len(ledger_deleted) != 100
        or len(ledger_zeroed) != 35
        or not ledger_deleted <= missing
        or ledger_zeroed != zeroed
        or len(missing - ledger_deleted) != 2
    ):
        raise ContentLossError("historical fixture does not match the 135-path recovery ledger")


def write_reports(report: dict[str, Any], *, json_out: Path, markdown_out: Path) -> None:
    canonical = json.dumps(
        report,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n"
    json_out.write_text(canonical, encoding="utf-8")
    if report["verdict"] == "ERROR":
        lines = [
            "# AIWerk content-loss guard: ERROR",
            "",
            f"- Guard: `{report['guard_version']}`",
            f"- Error: {report['error']['message']}",
            f"- Reason codes: {', '.join(report['reason_codes'])}",
            "",
        ]
    else:
        lines = [
            f"# AIWerk content-loss guard: {report['verdict']}",
            "",
            f"- Authority: `{report['authority_ref']}`",
            f"- Target tree: `{report['target_tree']}`",
            f"- Upstream: `{report['upstream_sha']}`",
            f"- Missing paths: {len(report['measurements']['missing_paths'])}",
            f"- Marker zero transitions: {len(report['measurements']['marker_zero_transitions'])}",
            f"- Reason codes: {', '.join(report['reason_codes']) or 'none'}",
            "",
        ]
    markdown_out.write_text("\n".join(lines), encoding="utf-8")


def _error_report(args: argparse.Namespace, exc: ContentLossError) -> dict[str, Any]:
    if args.command == "check-pr":
        inputs = {"active": args.base, "target": args.head, "upstream": args.upstream}
    elif args.command == "check-range":
        inputs = {"active": args.active, "target": args.target, "upstream": args.upstream}
    else:
        inputs = {
            "active": _HISTORICAL_ACTIVE,
            "target": _HISTORICAL_TARGET,
            "upstream": _HISTORICAL_UPSTREAM,
        }
    return {
        "authority_blobs": {},
        "authority_ref": None,
        "completeness": {
            "all_selectors_evaluated": False,
            "authority_loaded": False,
            "git_objects_complete": False,
            "reports_complete": True,
            "upstream_classification_complete": False,
        },
        "error": {"code": "EVIDENCE_ERROR", "message": str(exc)},
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "guard_version": _GUARD_VERSION,
        "input_refs": inputs,
        "mode": args.command,
        "reason_codes": ["EVIDENCE_ERROR"],
        "schema_version": 1,
        "verdict": "ERROR",
    }


def _cli() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("check-range", "check-pr"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--repo", type=Path, default=Path.cwd())
        cmd.add_argument("--active" if name == "check-range" else "--base", required=True)
        cmd.add_argument("--target" if name == "check-range" else "--head", required=True)
        cmd.add_argument("--upstream", required=True)
        cmd.add_argument("--pr-number", type=int)
        cmd.add_argument("--json-out", type=Path, required=True)
        cmd.add_argument("--markdown-out", type=Path, required=True)
    historical = sub.add_parser("historical-self-test")
    historical.add_argument("--repo", type=Path, default=Path.cwd())
    historical.add_argument("--json-out", type=Path, required=True)
    historical.add_argument("--markdown-out", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _cli().parse_args(argv)
    try:
        if args.command == "historical-self-test":
            report = evaluate_range(
                args.repo,
                _HISTORICAL_ACTIVE,
                _HISTORICAL_TARGET,
                _HISTORICAL_UPSTREAM,
                pr_number=None,
            )
            authority_sha = _resolve(args.repo, "HEAD", "commit")
            authority_entries = _tree_entries(args.repo, authority_sha)
            baseline, _retirements, _authority_blobs = _load_authority(
                args.repo, authority_sha, authority_entries
            )
            validate_historical_recovery_bijection(
                report, baseline["recovery_ledger"]
            )
            write_reports(report, json_out=args.json_out, markdown_out=args.markdown_out)
            print("Historical fixture correctly blocked known AIWerk losses")
            return 0
        active = args.active if args.command == "check-range" else args.base
        target = args.target if args.command == "check-range" else make_effective_merge_tree(
            args.repo, args.base, args.head
        )
        report = evaluate_range(
            args.repo,
            active,
            target,
            args.upstream,
            pr_number=args.pr_number,
        )
        if args.command == "check-pr":
            head_sha = _resolve(args.repo, args.head, "commit")
            report["mode"] = "pull-request"
            report["candidate_head_sha"] = head_sha
            report["candidate_head_parents"] = _commit_parents(args.repo, head_sha)
            report["effective_target"] = {"commit": None, "tree": report["target_tree"]}
            report["input_refs"] = {
                "active": _resolve(args.repo, args.base, "commit"),
                "target": head_sha,
                "upstream": _resolve(args.repo, args.upstream, "commit"),
            }
        write_reports(report, json_out=args.json_out, markdown_out=args.markdown_out)
        print(f"AIWerk content-loss guard: {report['verdict']}")
        return 0 if report["verdict"] == "PASS" else 2
    except ContentLossError as exc:
        error_report = _error_report(args, exc)
        write_reports(error_report, json_out=args.json_out, markdown_out=args.markdown_out)
        print(f"AIWerk content-loss guard ERROR: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
