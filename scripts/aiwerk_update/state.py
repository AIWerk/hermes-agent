"""Durable, resumable run state for the maintained updater."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any

from .contract import _atomic_write, canonical_bytes, canonical_sha256


_STAGES = ("execute", "control", "product", "publication", "artifact", "handoff")


class StateError(RuntimeError):
    pass


class RunStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.run_id = root.name

    @classmethod
    def create(
        cls,
        refresh_root: Path,
        *,
        run_id: str,
        request: dict[str, Any],
        authority: dict[str, Any],
    ) -> "RunStore":
        if not run_id or "/" in run_id or run_id in {".", ".."}:
            raise StateError("invalid run_id")
        root = refresh_root / run_id
        try:
            root.mkdir(parents=True, mode=0o700)
        except FileExistsError as exc:
            raise StateError("run already exists") from exc
        store = cls(root)
        _atomic_write(root / "request.json", canonical_bytes(request))
        _atomic_write(root / "authority.json", canonical_bytes(authority))
        store._write_state(
            {
                "schema_version": 1,
                "run_id": run_id,
                "phase": "PLANNED",
                "completed": [],
                "request_sha256": canonical_sha256(request),
                "authority_sha256": canonical_sha256(authority),
            }
        )
        store._append_event("run-created", {"phase": "PLANNED"})
        return store

    @classmethod
    def open(cls, root: Path) -> "RunStore":
        if not root.is_dir() or not (root / "state.json").is_file():
            raise StateError("run state is unavailable")
        store = cls(root)
        state = store._read_state()
        if state.get("run_id") != root.name:
            raise StateError("run identity mismatch")
        store._verify_bound_inputs(state)
        return store

    def _canonical_object(self, name: str) -> tuple[dict[str, Any], str]:
        path = self.root / name
        try:
            raw = path.read_bytes()
            value = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            raise StateError(f"bound {name} is invalid: {exc}") from exc
        if not isinstance(value, dict) or raw != canonical_bytes(value):
            raise StateError(f"bound {name} is not a canonical object")
        return value, canonical_sha256(value)

    def _verify_bound_inputs(self, state: dict[str, Any]) -> None:
        _request, request_sha = self._canonical_object("request.json")
        _authority, authority_sha = self._canonical_object("authority.json")
        if request_sha != state.get("request_sha256"):
            raise StateError("request authority hash mismatch")
        if authority_sha != state.get("authority_sha256"):
            raise StateError("repository authority hash mismatch")
        if "candidate_sha256" not in state:
            return
        candidate, candidate_sha = self._canonical_object("candidate.json")
        _config, config_sha = self._canonical_object("execution-config.json")
        if candidate_sha != state.get("candidate_sha256"):
            raise StateError("candidate manifest hash mismatch")
        if config_sha != state.get("execution_config_sha256"):
            raise StateError("execution configuration hash mismatch")
        repo = self.root / "candidate"
        probes = {}
        for name, args in (
            ("commit", ("rev-parse", "HEAD")),
            ("tree", ("rev-parse", "HEAD^{tree}")),
            ("status", ("status", "--porcelain=v1", "--untracked-files=no")),
        ):
            result = subprocess.run(
                ["git", *args],
                cwd=repo,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if result.returncode != 0:
                raise StateError(f"qualified candidate {name} probe failed")
            probes[name] = result.stdout.strip()
        if (
            probes["commit"] != state.get("qualified_commit")
            or probes["tree"] != state.get("qualified_tree")
            or probes["status"]
            or candidate.get("candidate_commit") != probes["commit"]
            or candidate.get("candidate_tree") != probes["tree"]
        ):
            raise StateError("qualified candidate identity mismatch")

    def bind_execution_inputs(
        self,
        *,
        candidate: dict[str, Any],
        execution_config: dict[str, Any],
        qualified_commit: str,
        qualified_tree: str,
    ) -> None:
        state = self._read_state()
        if state.get("phase") not in {"PLANNED", "PREFLIGHT_PASS"} or (self.root / "execution.claim").exists():
            raise StateError("execution inputs bind only before the execution claim")
        actual_candidate, candidate_sha = self._canonical_object("candidate.json")
        actual_config, config_sha = self._canonical_object("execution-config.json")
        if actual_candidate != candidate or actual_config != execution_config:
            raise StateError("execution input bytes differ from supplied binding")
        request, _request_sha = self._canonical_object("request.json")
        expected_config = request.get("execution_config_sha256")
        if expected_config is not None and expected_config != config_sha:
            raise StateError("execution configuration differs from request authority")
        state.update(
            candidate_sha256=candidate_sha,
            execution_config_sha256=config_sha,
            qualified_commit=qualified_commit,
            qualified_tree=qualified_tree,
        )
        self._verify_bound_inputs(state)
        self._write_state(state)
        self._append_event("execution-inputs-bound", {"qualified_tree": qualified_tree})

    def _read_state(self) -> dict[str, Any]:
        try:
            value = json.loads((self.root / "state.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StateError(f"invalid run state: {exc}") from exc
        if not isinstance(value, dict):
            raise StateError("run state must be an object")
        return value

    def _write_state(self, value: dict[str, Any]) -> None:
        _atomic_write(self.root / "state.json", canonical_bytes(value))

    def _append_event(self, kind: str, payload: dict[str, Any]) -> None:
        event = {"kind": kind, "run_id": self.run_id, "payload": payload}
        path = self.root / "events.jsonl"
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            data = canonical_bytes(event)
            offset = 0
            while offset < len(data):
                written = os.write(fd, data[offset:])
                if written <= 0:
                    raise StateError("event journal made no write progress")
                offset += written
            os.fsync(fd)
        finally:
            os.close(fd)

    def record_preflight(self, receipt: dict[str, Any]) -> None:
        state = self._read_state()
        if state["phase"] != "PLANNED":
            raise StateError("preflight can be recorded only from PLANNED")
        _atomic_write(self.root / "preflight.json", canonical_bytes(receipt))
        state["phase"] = "PREFLIGHT_PASS" if receipt.get("verdict") == "PASS" else "PREFLIGHT_FAIL"
        self._write_state(state)
        self._append_event("preflight-recorded", {"verdict": receipt.get("verdict")})

    def claim_payload(self) -> dict[str, Any]:
        state = self._read_state()
        payload = {
            "schema_version": 1,
            "kind": "execution-claim",
            "run_id": self.run_id,
            "request_sha256": state["request_sha256"],
            "authority_sha256": state["authority_sha256"],
        }
        for key in (
            "candidate_sha256",
            "execution_config_sha256",
            "qualified_commit",
            "qualified_tree",
        ):
            if key in state:
                payload[key] = state[key]
        return payload

    def begin_execution(self) -> None:
        state = self._read_state()
        claim = self.root / "execution.claim"
        expected_claim = self.claim_payload()
        if claim.exists():
            try:
                actual_claim = json.loads(claim.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise StateError(f"execution claim is invalid: {exc}") from exc
            if actual_claim != expected_claim:
                raise StateError("execution claim identity mismatch")
            if state["phase"] == "EXECUTING":
                return
            if state["phase"] != "PREFLIGHT_PASS":
                raise StateError("execution claim exists outside a recoverable phase")
            state["phase"] = "EXECUTING"
            self._write_state(state)
            self._append_event("execution-claim-recovered", {})
            return
        if state["phase"] != "PREFLIGHT_PASS":
            raise StateError("execution requires a passing preflight")
        data = canonical_bytes(expected_claim)
        fd, temporary_name = tempfile.mkstemp(
            prefix=".execution.claim.", dir=self.root
        )
        temporary = Path(temporary_name)
        try:
            try:
                os.fchmod(fd, 0o600)
                offset = 0
                while offset < len(data):
                    written = os.write(fd, data[offset:])
                    if written <= 0:
                        raise StateError("execution claim made no write progress")
                    offset += written
                os.fsync(fd)
            finally:
                os.close(fd)
            try:
                os.link(temporary, claim)
            except FileExistsError as exc:
                raise StateError("execution claim already exists") from exc
            directory_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)
        state["phase"] = "EXECUTING"
        self._write_state(state)
        self._append_event("execution-started", {})

    def complete_stage(self, stage: str) -> None:
        if stage not in _STAGES[1:-1]:
            raise StateError("invalid completion stage")
        state = self._read_state()
        if state["phase"] != "EXECUTING":
            raise StateError("stage completion requires EXECUTING")
        completed = list(state.get("completed", []))
        expected = self.next_stage()
        if stage != expected:
            raise StateError(f"out-of-order stage: expected {expected}")
        completed.append(stage)
        state["completed"] = completed
        self._write_state(state)
        self._append_event("stage-completed", {"stage": stage})

    def finish_handoff(self, handoff: dict[str, Any]) -> None:
        state = self._read_state()
        if state["phase"] != "EXECUTING" or self.next_stage() != "handoff":
            raise StateError("handoff can finish only after artifact completion")
        activation = handoff.get("activation")
        proof_fields = (
            "installed_updater_source_identity",
            "installed_updater_wheel_identity",
            "installed_update_check_receipt_sha256",
            "extracted_target_preflight_receipt_sha256",
            "recovery_receipt_sha256",
        )
        proofs_valid = all(
            isinstance(handoff.get(field), str)
            and len(handoff[field]) == 64
            and all(character in "0123456789abcdef" for character in handoff[field])
            for field in proof_fields
        )
        if (
            handoff.get("status") != "HANDOFF_READY"
            or activation != "NOT_RUN_REQUIRES_SEPARATE_ATTILA_GO_AND_JEROME"
            or not proofs_valid
        ):
            raise StateError("handoff identity proof is incomplete or activation is blocked")
        _atomic_write(self.root / "local-handoff.json", canonical_bytes(handoff))
        final = {
            "schema_version": 1,
            "kind": "AIWERK_UPDATE_FINAL",
            "run_id": self.run_id,
            "status": "HANDOFF_READY",
            "activation": "NOT_RUN",
        }
        _atomic_write(self.root / "final.json", canonical_bytes(final))
        state["completed"] = [*state.get("completed", []), "handoff"]
        state["phase"] = "HANDOFF_READY"
        self._write_state(state)
        self._append_event("handoff-ready", {})
        manifest_rows = []
        for path in sorted(self.root.iterdir()):
            if not path.is_file() or path.name == "manifest.sha256":
                continue
            manifest_rows.append(
                f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}"
            )
        _atomic_write(
            self.root / "manifest.sha256",
            ("\n".join(manifest_rows) + "\n").encode("utf-8"),
        )

    def next_stage(self) -> str | None:
        state = self._read_state()
        if state["phase"] == "PREFLIGHT_PASS":
            return "execute"
        if state["phase"] != "EXECUTING":
            return None
        completed = set(state.get("completed", []))
        return next((stage for stage in _STAGES[1:] if stage not in completed), None)
