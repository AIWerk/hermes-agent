"""Stable command-line surface for updater-owned refreshes."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from typing import Sequence

from .artifact import ArtifactBuildConfig, ArtifactError, ExternalRuntimeArtifactBuilder
from .contract import ContractError, _atomic_write, canonical_bytes, canonical_sha256
from .preflight import Probe, RepositoryQualifier, collect_preflight, repository_preflight_probes
from .source import (
    OverlayManifest,
    capture_git_authority,
    prepare_upstream_first_candidate,
)
from .state import RunStore, StateError
from .publication import GitHubClient, GitHubPublisher, PublicationError
from .release import ReleaseError
from .workflow import execute_update


def _emit(value: dict, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, sort_keys=True, separators=(",", ":")))
    else:
        for key, item in value.items():
            print(f"{key}: {item}")


def _run_id(authority: dict[str, str]) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{authority['base_commit'][:12]}-{authority['target_commit'][:12]}"


def _read_object(path: Path) -> dict:
    try:
        value = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"JSON object unavailable: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ContractError(f"JSON object required: {path}")
    return value


def _execution_config(raw: dict) -> dict:
    required = {
        "schema_version",
        "repository",
        "required_checks",
        "workflow_path",
        "protected_controls",
        "approval_not_before",
        "approval_expires_at",
        "artifact",
    }
    if set(raw) != required or raw.get("schema_version") != 1:
        raise ContractError("execution configuration schema mismatch")
    if raw.get("repository") != "AIWerk/hermes-agent":
        raise ContractError("execution repository must be AIWerk/hermes-agent")
    for field in ("required_checks", "protected_controls"):
        values = raw.get(field)
        if (
            not isinstance(values, list)
            or not values
            or not all(isinstance(item, str) and item for item in values)
            or values != sorted(set(values))
        ):
            raise ContractError(f"execution configuration {field} must be sorted unique strings")
    if not isinstance(raw.get("workflow_path"), str) or not raw["workflow_path"]:
        raise ContractError("execution workflow path is required")
    if not isinstance(raw.get("artifact"), dict):
        raise ContractError("execution artifact configuration is required")
    for field in ("approval_not_before", "approval_expires_at"):
        try:
            value = datetime.fromisoformat(str(raw[field]).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ContractError(f"execution {field} is not ISO-8601") from exc
        if value.tzinfo is None or value.utcoffset() is None:
            raise ContractError(f"execution {field} must include timezone")
    return raw


def _source_input_hashes(repo: Path, commit: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for name in ("pyproject.toml", "uv.lock", "immutable-release-build.json"):
        completed = subprocess.run(
            ["git", "show", f"{commit}:{name}"],
            cwd=repo,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode != 0:
            raise ContractError(f"artifact source input unavailable: {name}")
        result[name] = hashlib.sha256(completed.stdout).hexdigest()
    return result


def _continue(store: RunStore, config: dict) -> dict:
    authority = _read_object(store.root / "authority.json")
    candidate = _read_object(store.root / "candidate.json")
    candidate_repo = store.root / "candidate"
    if not candidate_repo.is_dir() or candidate.get("status") != "CANDIDATE_READY":
        raise StateError("run candidate is unavailable for execution")
    candidate_commit = str(candidate["candidate_commit"])
    artifact_config = ArtifactBuildConfig.from_dict(
        config["artifact"],
        source_input_sha256=_source_input_hashes(candidate_repo, candidate_commit),
    )
    publisher = GitHubPublisher(
        client=GitHubClient(repository="AIWerk/hermes-agent"),
        required_checks=set(config["required_checks"]),
        workflow_path=config["workflow_path"],
    )
    return execute_update(
        store=store,
        candidate_repo=candidate_repo,
        base_commit=str(authority["base_commit"]),
        candidate_commit=candidate_commit,
        upstream_commit=str(authority["target_commit"]),
        protected_controls=set(config["protected_controls"]),
        publisher=publisher,
        artifact_builder=ExternalRuntimeArtifactBuilder(artifact_config),
        qualifier=RepositoryQualifier(),
        approval_not_before=datetime.fromisoformat(
            config["approval_not_before"].replace("Z", "+00:00")
        ),
        approval_expires_at=datetime.fromisoformat(
            config["approval_expires_at"].replace("Z", "+00:00")
        ),
    )


def _check(args: argparse.Namespace) -> int:
    authority = capture_git_authority(Path(args.repo), args.base_ref, args.target_ref)
    payload = {
        "schema_version": 1,
        "kind": "AIWERK_UPDATE_CHECK",
        "verdict": "OBSERVATION_ONLY",
        "ready_for_preflight": True,
        "update_needed": authority["base_commit"] != authority["target_commit"],
        "authority": authority,
    }
    _emit(payload, as_json=args.json)
    return 0


def _run(args: argparse.Namespace) -> int:
    if args.through != "local-handoff" or args.authorize != "source-publication":
        raise ContractError("run authority must be source-publication through local-handoff")
    repo = Path(args.repo).resolve()
    authority = capture_git_authority(repo, args.base_ref, args.target_ref)
    execution_config = None
    if not args.preflight_only:
        if not args.execution_config or not args.overlay_manifest:
            raise StateError("execution requires --execution-config and --overlay-manifest")
        execution_config = _execution_config(
            _read_object(Path(args.execution_config).resolve())
        )
    request = {
        "schema_version": 1,
        "repository": "AIWerk/hermes-agent",
        "through": args.through,
        "source_publication": True,
        "base_commit": authority["base_commit"],
        "target_commit": authority["target_commit"],
    }
    if execution_config is not None:
        request["execution_config_sha256"] = canonical_sha256(execution_config)
    run_id = args.run_id or _run_id(authority)
    if args.preflight_profile == "repository" and not args.overlay_manifest:
        raise ContractError("repository preflight requires an upstream-first overlay manifest")
    store = RunStore.create(
        Path(args.refresh_root).resolve(),
        run_id=run_id,
        request={**request, "request_sha256": canonical_sha256(request)},
        authority=authority,
    )
    if execution_config is not None:
        _atomic_write(
            store.root / "execution-config.json", canonical_bytes(execution_config)
        )
    candidate_repo = repo
    candidate_commit = authority["target_commit"]
    if args.overlay_manifest:
        manifest = OverlayManifest.read(Path(args.overlay_manifest).resolve())
        if manifest.upstream_commit != authority["target_commit"]:
            raise ContractError("overlay manifest upstream commit differs from captured target")
        candidate = prepare_upstream_first_candidate(
            repo,
            store.root / "candidate",
            manifest,
        )
        _atomic_write(store.root / "candidate.json", canonical_bytes(candidate))
        if candidate["status"] != "CANDIDATE_READY":
            receipt = {
                "schema_version": 1,
                "kind": "AIWERK_UPDATE_PREFLIGHT",
                "verdict": "FAIL",
                "results": [],
                "failures": [
                    {
                        "name": "candidate-reconciliation",
                        "status": candidate["status"],
                        "conflicting_paths": candidate.get("conflicting_paths", []),
                    }
                ],
                "mutation_claim_created": False,
            }
            _atomic_write(store.root / "preflight.json", canonical_bytes(receipt))
            store.record_preflight(receipt)
            _emit(
                {
                    "run_id": run_id,
                    "run_root": str(store.root),
                    "state": "PREFLIGHT_FAIL",
                    "verdict": "FAIL",
                },
                as_json=True,
            )
            return 1
        candidate_repo = store.root / "candidate"
        candidate_commit = candidate["candidate_commit"]
        if execution_config is not None:
            store.bind_execution_inputs(
                candidate=candidate,
                execution_config=execution_config,
                qualified_commit=str(candidate["candidate_commit"]),
                qualified_tree=str(candidate["candidate_tree"]),
            )
    if args.preflight_profile == "repository":
        probes = repository_preflight_probes(
            base_commit=authority["base_commit"],
            candidate_commit=candidate_commit,
            upstream_commit=authority["target_commit"],
            output_dir=store.root,
            include_canonical_suite=False,
            include_js=False,
            include_content_loss=False,
        )
    else:
        probes = (
            Probe("base-commit", ("git", "cat-file", "-e", f"{authority['base_commit']}^{{commit}}")),
            Probe("target-commit", ("git", "cat-file", "-e", f"{candidate_commit}^{{commit}}")),
            Probe(
                "diff-check",
                ("git", "diff", "--check", f"{authority['base_commit']}...{candidate_commit}"),
            ),
        )
    receipt = collect_preflight(
        probes=probes,
        cwd=candidate_repo,
        output=store.root / "preflight.json",
    )
    store.record_preflight(receipt)
    phase = store._read_state()["phase"]
    payload = {"run_id": run_id, "run_root": str(store.root), "state": phase, "verdict": receipt["verdict"]}
    if receipt["verdict"] != "PASS":
        _emit(payload, as_json=True)
        return 1
    if args.preflight_only:
        _emit(payload, as_json=True)
        return 0
    if execution_config is None:
        raise StateError("execution configuration was not bound")
    final = _continue(store, execution_config)
    _emit(
        {
            "run_id": run_id,
            "run_root": str(store.root),
            "state": store._read_state()["phase"],
            "verdict": "PASS",
            "handoff": final,
        },
        as_json=True,
    )
    return 0


def _resolve_run_root(args: argparse.Namespace) -> Path:
    if getattr(args, "run_root", None):
        return Path(args.run_root).resolve()
    if getattr(args, "run_id", None) and getattr(args, "refresh_root", None):
        return (Path(args.refresh_root).resolve() / args.run_id).resolve()
    raise ContractError("run locator requires --run-root or --refresh-root plus --run-id")


def _status(args: argparse.Namespace) -> int:
    store = RunStore.open(_resolve_run_root(args))
    state = store._read_state()
    payload = {**state, "run_root": str(store.root), "next_stage": store.next_stage()}
    _emit(payload, as_json=args.json)
    return 0


def _resume(args: argparse.Namespace) -> int:
    store = RunStore.open(_resolve_run_root(args))
    state = store._read_state()
    required_bindings = {
        "candidate_sha256",
        "execution_config_sha256",
        "qualified_commit",
        "qualified_tree",
    }
    if not required_bindings <= set(state):
        raise StateError("run is not execution-bound and cannot be resumed")
    config = _execution_config(_read_object(store.root / "execution-config.json"))
    result = _continue(store, config)
    _emit(
        {
            "run_id": store.run_id,
            "run_root": str(store.root),
            "state": store._read_state()["phase"],
            "verdict": "PASS",
            "handoff": result,
        },
        as_json=True,
    )
    return 0


def _evidence(args: argparse.Namespace) -> int:
    store = RunStore.open(_resolve_run_root(args))
    manifest = store.root / "manifest.sha256"
    verified: list[str] = []
    phase = store._read_state()["phase"]
    if phase == "HANDOFF_READY" and not manifest.is_file():
        raise StateError("terminal run evidence manifest is missing")
    if manifest.is_file():
        raw = manifest.read_bytes()
        if not raw or not raw.endswith(b"\n"):
            raise StateError("run evidence manifest is empty or noncanonical")
        lines = raw.decode("utf-8").splitlines()
        for line in lines:
            digest, separator, name = line.partition("  ")
            path = store.root / name
            if (
                separator != "  "
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
                or not name
                or Path(name).name != name
                or name == "manifest.sha256"
                or not path.is_file()
                or hashlib.sha256(path.read_bytes()).hexdigest() != digest
            ):
                raise StateError(f"run evidence manifest mismatch: {name!r}")
            verified.append(name)
        expected = sorted(
            path.name
            for path in store.root.iterdir()
            if path.is_file() and path.name != "manifest.sha256"
        )
        if verified != sorted(set(verified)) or verified != expected:
            raise StateError("run evidence manifest is partial, duplicate, extra, or unsorted")
    payload = {
        "schema_version": 1,
        "kind": "AIWERK_UPDATE_EVIDENCE",
        "run_id": store.run_id,
        "run_root": str(store.root),
        "phase": store._read_state()["phase"],
        "next_stage": store.next_stage(),
        "manifest_present": manifest.is_file(),
        "verified_files": verified,
    }
    _emit(payload, as_json=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aiwerk-runtime-integrate")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check")
    check.add_argument("--repo", required=True)
    check.add_argument("--base-ref", default="origin/main")
    check.add_argument("--target-ref", default="upstream/main")
    check.add_argument("--json", action="store_true")
    check.set_defaults(handler=_check)

    run = sub.add_parser("run")
    run.add_argument("--repo", required=True)
    run.add_argument("--base-ref", default="origin/main")
    run.add_argument("--target-ref", default="upstream/main")
    run.add_argument("--refresh-root", required=True)
    run.add_argument("--through", required=True)
    run.add_argument("--authorize", required=True)
    run.add_argument("--run-id")
    run.add_argument("--overlay-manifest")
    run.add_argument("--execution-config")
    run.add_argument("--preflight-profile", choices=("minimal", "repository"), default="repository")
    run.add_argument("--preflight-only", action="store_true")
    run.set_defaults(handler=_run)

    status = sub.add_parser("status")
    status.add_argument("--run-root")
    status.add_argument("--refresh-root")
    status.add_argument("--run-id")
    status.add_argument("--json", action="store_true")
    status.set_defaults(handler=_status)

    resume = sub.add_parser("resume")
    resume.add_argument("--run-root")
    resume.add_argument("--refresh-root")
    resume.add_argument("--run-id")
    resume.set_defaults(handler=_resume)

    evidence = sub.add_parser("evidence")
    evidence.add_argument("--run-root")
    evidence.add_argument("--refresh-root")
    evidence.add_argument("--run-id")
    evidence.set_defaults(handler=_evidence)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (
        ArtifactError,
        ContractError,
        PublicationError,
        ReleaseError,
        StateError,
        OSError,
    ) as exc:
        print(json.dumps({"verdict": "ERROR", "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
