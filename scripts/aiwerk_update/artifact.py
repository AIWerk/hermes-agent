"""Build-package verification without activation or runtime mutation."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tarfile
import tomllib
from typing import Any, Callable
import zipfile

from .contract import canonical_bytes


class ArtifactError(RuntimeError):
    pass


ArtifactRunner = Callable[[tuple[str, ...], Path], None]
ArtifactVerifier = Callable[..., dict[str, Any]]


def _default_runner(argv: tuple[str, ...], cwd: Path) -> None:
    result = subprocess.run(
        list(argv),
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise ArtifactError(
            f"artifact command failed rc={result.returncode} argv={argv!r}: "
            f"{result.stderr[-2000:]}"
        )


def _digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ArtifactError(f"{label} must be a lowercase SHA-256")
    return value


@dataclass(frozen=True, slots=True)
class ArtifactBuildConfig:
    payload_builder: Path
    payload_builder_sha256: str
    runtime_builder: Path
    runtime_builder_sha256: str
    npm: Path
    npm_sha256: str
    wheelhouse: Path
    wheel_lock: Path
    python_prefix: Path
    python_sha256: str
    epoch: int
    source_input_sha256: dict[str, str]

    @classmethod
    def from_dict(
        cls, raw: dict[str, Any], *, source_input_sha256: dict[str, str]
    ) -> "ArtifactBuildConfig":
        required = {
            "payload_builder",
            "payload_builder_sha256",
            "runtime_builder",
            "runtime_builder_sha256",
            "npm",
            "npm_sha256",
            "wheelhouse",
            "wheel_lock",
            "python_prefix",
            "python_sha256",
            "epoch",
        }
        if not isinstance(raw, dict) or set(raw) != required:
            raise ArtifactError("artifact build configuration schema mismatch")
        path_fields = {
            name: Path(str(raw[name]))
            for name in (
                "payload_builder",
                "runtime_builder",
                "npm",
                "wheelhouse",
                "wheel_lock",
                "python_prefix",
            )
        }
        return cls(
            **path_fields,
            payload_builder_sha256=str(raw["payload_builder_sha256"]),
            runtime_builder_sha256=str(raw["runtime_builder_sha256"]),
            npm_sha256=str(raw["npm_sha256"]),
            python_sha256=str(raw["python_sha256"]),
            epoch=raw["epoch"],
            source_input_sha256=dict(source_input_sha256),
        )

    def __post_init__(self) -> None:
        for label, value in (
            ("payload_builder_sha256", self.payload_builder_sha256),
            ("runtime_builder_sha256", self.runtime_builder_sha256),
            ("npm_sha256", self.npm_sha256),
            ("python_sha256", self.python_sha256),
        ):
            _digest(value, label)
        if not isinstance(self.epoch, int) or self.epoch < 0:
            raise ArtifactError("artifact epoch must be a nonnegative integer")
        if set(self.source_input_sha256) != {
            "pyproject.toml",
            "uv.lock",
            "immutable-release-build.json",
        }:
            raise ArtifactError("artifact source input binding is incomplete")
        for name, value in self.source_input_sha256.items():
            _digest(value, f"source_input_sha256[{name}]")

        for label, path in (
            ("payload builder", self.payload_builder),
            ("runtime builder", self.runtime_builder),
            ("npm", self.npm),
        ):
            if not path.is_absolute() or not path.is_file() or path.is_symlink():
                raise ArtifactError(f"{label} must be an absolute regular non-symlink file")
        for path, expected, label in (
            (self.payload_builder, self.payload_builder_sha256, "payload builder"),
            (self.runtime_builder, self.runtime_builder_sha256, "runtime builder"),
            (self.npm, self.npm_sha256, "npm"),
        ):
            if _sha_file(path) != expected:
                raise ArtifactError(f"{label} hash mismatch")
        if not self.wheelhouse.is_absolute() or not self.wheelhouse.is_dir():
            raise ArtifactError("wheelhouse must be an absolute directory")
        if not self.wheel_lock.is_absolute() or not self.wheel_lock.is_file():
            raise ArtifactError("wheel lock must be an absolute regular file")
        if not self.python_prefix.is_absolute() or not self.python_prefix.is_dir():
            raise ArtifactError("Python prefix must be an absolute directory")


class ExternalRuntimeArtifactBuilder:
    """Compose the audited payload compiler and build-only runtime builder."""

    def __init__(
        self,
        config: ArtifactBuildConfig,
        *,
        runner: ArtifactRunner = _default_runner,
        verifier: ArtifactVerifier | None = None,
    ) -> None:
        self.config = config
        self.runner = runner
        self.verifier = verifier or verify_artifact_package

    def _git(self, root: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if result.returncode != 0:
            raise ArtifactError(f"Git artifact preflight failed: {result.stderr[-2000:]}")
        return result.stdout.strip()

    def _payload_provenance(self, wheel: Path) -> dict[str, Any]:
        try:
            with zipfile.ZipFile(wheel) as archive:
                names = [
                    name
                    for name in archive.namelist()
                    if name.endswith(".dist-info/aiwerk-payload-provenance.json")
                ]
                if len(names) != 1:
                    raise ArtifactError("payload wheel provenance is missing or ambiguous")
                value = json.loads(archive.read(names[0]))
        except (OSError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
            raise ArtifactError(f"payload wheel invalid: {exc}") from exc
        if not isinstance(value, dict):
            raise ArtifactError("payload wheel provenance must be an object")
        return value

    def build_verified(
        self,
        *,
        source_repo: Path,
        source_commit: str,
        source_tree: str,
        output: Path,
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        source_repo = source_repo.resolve(strict=True)
        output = output.absolute()
        if output.is_symlink():
            raise ArtifactError("artifact output cannot be a symlink")
        if output.exists():
            if not output.is_dir():
                raise ArtifactError("artifact output exists with wrong type")
            return self.verifier(
                output,
                expected_commit=source_commit,
                expected_git_tree=source_tree,
            )
        if not isinstance(evidence, dict) or set(evidence) != {
            "qualification_sha256",
            "detector_sha256",
        }:
            raise ArtifactError("artifact evidence binding is incomplete")
        suite_sha256 = _digest(
            evidence["qualification_sha256"], "qualification_sha256"
        )
        detector_sha256 = evidence["detector_sha256"]
        if not isinstance(detector_sha256, dict) or set(detector_sha256) != {
            "supply-chain",
            "osv",
        }:
            raise ArtifactError("exactly supply-chain and osv evidence is required")
        for name, value in detector_sha256.items():
            _digest(value, f"detector_sha256[{name}]")
        if self._git(source_repo, "rev-parse", "HEAD") != source_commit:
            raise ArtifactError("artifact source HEAD mismatch")
        if self._git(source_repo, "rev-parse", "HEAD^{tree}") != source_tree:
            raise ArtifactError("artifact source tree mismatch")
        if self._git(source_repo, "status", "--porcelain=v1", "--untracked-files=no"):
            raise ArtifactError("artifact source tracked status is dirty")
        for name, expected in self.config.source_input_sha256.items():
            path = source_repo / name
            if not path.is_file() or path.is_symlink() or _sha_file(path) != expected:
                raise ArtifactError(f"artifact dependency input drift: {name}")
        try:
            project = tomllib.loads(
                (source_repo / "pyproject.toml").read_text(encoding="utf-8")
            )
            if project["project"]["name"] != "hermes-agent":
                raise KeyError("name")
            version = project["project"]["version"]
        except (OSError, UnicodeError, tomllib.TOMLDecodeError, KeyError, TypeError) as exc:
            raise ArtifactError("Hermes project metadata unavailable") from exc
        if not isinstance(version, str) or not version:
            raise ArtifactError("Hermes project version invalid")

        inputs = output.parent / f".{output.name}.inputs"
        if inputs.exists() or inputs.is_symlink():
            raise ArtifactError("artifact input workspace already exists")
        inputs.mkdir(parents=True)
        self.runner((str(self.config.npm), "ci", "--ignore-scripts"), source_repo)
        self.runner(
            (str(self.config.npm), "run", "build", "--workspace", "web"),
            source_repo,
        )
        web_dist = source_repo / "hermes_cli/web_dist"
        if not (web_dist / "index.html").is_file():
            raise ArtifactError("dashboard build artifact is absent")
        wheel_name = f"hermes_agent-{version}-py3-none-any.whl"
        payloads: list[Path] = []
        for name in ("payload-one", "payload-two"):
            destination = inputs / name / wheel_name
            destination.parent.mkdir()
            self.runner(
                (
                    str(self.config.payload_builder),
                    "--source-root",
                    str(source_repo),
                    "--source-commit",
                    source_commit,
                    "--source-tree",
                    source_tree,
                    "--web-dist",
                    str(web_dist),
                    "--output",
                    str(destination),
                    "--epoch",
                    str(self.config.epoch),
                ),
                source_repo,
            )
            payloads.append(destination)
        if payloads[0].read_bytes() != payloads[1].read_bytes():
            raise ArtifactError("payload wheel is not reproducible across roots")
        provenance = self._payload_provenance(payloads[0])
        inventory_sha = provenance.get("tracked_git_object_inventory_sha256")
        if (
            provenance.get("source_commit") != source_commit
            or provenance.get("source_tree") != source_tree
            or not isinstance(inventory_sha, str)
            or len(inventory_sha) != 64
        ):
            raise ArtifactError("payload provenance source identity mismatch")

        try:
            lock_raw = self.config.wheel_lock.read_bytes()
            lock = json.loads(lock_raw)
        except (OSError, json.JSONDecodeError) as exc:
            raise ArtifactError("wheel lock unavailable") from exc
        if (
            not isinstance(lock, dict)
            or set(lock) != {"schema", "kind", "wheels"}
            or lock.get("schema") != 1
            or lock.get("kind") != "aiwerk-wheel-lock"
            or not isinstance(lock.get("wheels"), list)
        ):
            raise ArtifactError("wheel lock schema mismatch")
        wheels: list[Path] = []
        rows: list[dict[str, str]] = []
        for row in lock["wheels"]:
            if not isinstance(row, dict) or set(row) != {"name", "sha256"}:
                raise ArtifactError("wheel lock entry schema mismatch")
            name = row["name"]
            if not isinstance(name, str) or not name.endswith(".whl"):
                raise ArtifactError("wheel lock filename invalid")
            if name.startswith("hermes_agent-"):
                continue
            path = self.config.wheelhouse / name
            if not path.is_file() or path.is_symlink() or _sha_file(path) != row["sha256"]:
                raise ArtifactError(f"locked dependency wheel unavailable: {name}")
            wheels.append(path)
            rows.append({"name": name, "sha256": row["sha256"]})
        payload_sha = _sha_file(payloads[0])
        wheels.append(payloads[0])
        rows.append({"name": wheel_name, "sha256": payload_sha})
        rows.sort(key=lambda row: row["name"])
        generated_lock = inputs / "wheel-lock.json"
        generated_lock.write_bytes(
            canonical_bytes({"schema": 1, "kind": "aiwerk-wheel-lock", "wheels": rows})
        )
        qualification = inputs / "qualification-receipt.json"
        qualification.write_bytes(
            canonical_bytes(
                {
                    "schema": 1,
                    "kind": "aiwerk-qualification-receipt",
                    "source_commit": source_commit,
                    "source_tree": inventory_sha,
                    "status": "PASS",
                    "suite_sha256": suite_sha256,
                }
            )
        )
        detector_paths: list[Path] = []
        for index, name in enumerate(sorted(detector_sha256), 1):
            path = inputs / f"detector-{index}.json"
            path.write_bytes(
                canonical_bytes(
                    {
                        "schema": 1,
                        "kind": "aiwerk-detector-receipt",
                        "source_commit": source_commit,
                        "source_tree": inventory_sha,
                        "detector": name,
                        "status": "PASS",
                        "result_sha256": detector_sha256[name],
                    }
                )
            )
            detector_paths.append(path)
        argv: list[str] = [str(self.config.runtime_builder)]
        for path in sorted(wheels, key=lambda item: item.name):
            argv.extend(("--wheel", str(path)))
        argv.extend(
            (
                "--wheel-lock",
                str(generated_lock),
                "--output",
                str(output),
                "--release-id",
                source_commit,
                "--source-commit",
                source_commit,
                "--source-tree",
                inventory_sha,
                "--entry-module",
                "hermes_cli.main",
                "--epoch",
                str(self.config.epoch),
                "--python-prefix",
                str(self.config.python_prefix),
                "--python-sha256",
                self.config.python_sha256,
                "--qualification-receipt",
                str(qualification),
            )
        )
        for path in detector_paths:
            argv.extend(("--detector-receipt", str(path)))
        argv.extend(("--migration-class", "forward_only"))
        self.runner(tuple(argv), source_repo)
        if self._git(source_repo, "status", "--porcelain=v1", "--untracked-files=no"):
            raise ArtifactError("artifact tools modified tracked source bytes")
        return self.verifier(
            output,
            expected_commit=source_commit,
            expected_git_tree=source_tree,
        )


def _sha_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb", buffering=0) as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _pairs(rows: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in rows:
        if key in result:
            raise ArtifactError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _document(path: Path, *, kind: str) -> tuple[bytes, dict[str, Any]]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ArtifactError(f"artifact JSON unavailable: {path.name}: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema") != 1 or value.get("kind") != kind:
        raise ArtifactError(f"artifact JSON kind mismatch: {path.name}")
    if raw != canonical_bytes(value):
        raise ArtifactError(f"artifact JSON is not canonical: {path.name}")
    return raw, value


def _manifest_entries(value: dict[str, Any], *, label: str) -> list[dict[str, Any]]:
    entries = value.get("entries")
    if not isinstance(entries, list):
        raise ArtifactError(f"{label} entries missing")
    normalized: list[dict[str, Any]] = []
    names: list[str] = []
    for row in entries:
        if not isinstance(row, dict) or set(row) != {"path", "type", "mode", "size", "sha256"}:
            raise ArtifactError(f"{label} entry schema mismatch")
        name = row["path"]
        if (
            not isinstance(name, str)
            or not name
            or name.startswith("/")
            or "\\" in name
            or any(part in {"", ".", ".."} for part in name.split("/"))
        ):
            raise ArtifactError(f"{label} entry path unsafe")
        if row["type"] not in {"file", "dir"} or not isinstance(row["mode"], int):
            raise ArtifactError(f"{label} entry type or mode invalid")
        if row["type"] == "dir":
            if row["size"] != 0 or row["sha256"] is not None:
                raise ArtifactError(f"{label} directory entry invalid")
        elif (
            not isinstance(row["size"], int)
            or row["size"] < 0
            or not isinstance(row["sha256"], str)
            or len(row["sha256"]) != 64
        ):
            raise ArtifactError(f"{label} file entry invalid")
        names.append(name)
        normalized.append(row)
    if names != sorted(names) or len(names) != len(set(names)):
        raise ArtifactError(f"{label} entries are not canonical and unique")
    return normalized


def _inventory(root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for parent, directories, files in os.walk(root, topdown=True, followlinks=False):
        directories.sort()
        files.sort()
        for name in [*directories, *files]:
            path = Path(parent) / name
            relative = path.relative_to(root).as_posix()
            status = path.lstat()
            if stat.S_ISLNK(status.st_mode):
                raise ArtifactError(f"artifact runtime symlink rejected: {relative}")
            if stat.S_ISDIR(status.st_mode):
                rows.append(
                    {
                        "path": relative,
                        "type": "dir",
                        "mode": stat.S_IMODE(status.st_mode),
                        "size": 0,
                        "sha256": None,
                    }
                )
            elif stat.S_ISREG(status.st_mode):
                rows.append(
                    {
                        "path": relative,
                        "type": "file",
                        "mode": stat.S_IMODE(status.st_mode),
                        "size": status.st_size,
                        "sha256": _sha_file(path),
                    }
                )
            else:
                raise ArtifactError(f"artifact runtime special file rejected: {relative}")
    return sorted(rows, key=lambda row: row["path"])


def _verify_archive(path: Path, entries: list[dict[str, Any]]) -> None:
    expected = {row["path"]: row for row in entries}
    seen: set[str] = set()
    try:
        with tarfile.open(path, mode="r:*") as archive:
            for member in archive.getmembers():
                name = member.name.removeprefix("./")
                if name not in expected or name in seen:
                    raise ArtifactError("archive/manifest are not a bijection")
                row = expected[name]
                seen.add(name)
                if member.issym() or member.islnk() or member.isdev() or member.isfifo():
                    raise ArtifactError("archive contains unsafe member type")
                if stat.S_IMODE(member.mode) != row["mode"]:
                    raise ArtifactError("archive member mode mismatch")
                if row["type"] == "dir":
                    if not member.isdir():
                        raise ArtifactError("archive member type mismatch")
                    continue
                if not member.isfile() or member.size != row["size"]:
                    raise ArtifactError("archive member size/type mismatch")
                stream = archive.extractfile(member)
                if stream is None:
                    raise ArtifactError("archive member body missing")
                digest = hashlib.sha256()
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
                if digest.hexdigest() != row["sha256"]:
                    raise ArtifactError("archive member content mismatch")
    except (OSError, tarfile.TarError) as exc:
        raise ArtifactError(f"archive invalid: {exc}") from exc
    if seen != set(expected):
        raise ArtifactError("archive/manifest are not a bijection")


def verify_artifact_package(
    root: Path,
    *,
    expected_commit: str,
    expected_git_tree: str,
) -> dict[str, Any]:
    root = root.absolute()
    if root.is_symlink() or not root.is_dir() or root.resolve(strict=True) != root:
        raise ArtifactError("artifact package root must be a physical canonical directory")
    if len(expected_commit) != 40 or len(expected_git_tree) != 40:
        raise ArtifactError("expected source Git identity malformed")
    required = {
        "runtime",
        "artifact.tar",
        "payload-manifest.json",
        "aux-manifest.json",
        "release.json",
        "publication.json",
        "approved-pin.json",
        "updater-manifest.json",
        "qualification-receipt.json",
        "detector-1.json",
        "detector-2.json",
    }
    actual = {path.name for path in root.iterdir()}
    if actual != required:
        raise ArtifactError(
            f"artifact package members differ: missing={sorted(required-actual)} extra={sorted(actual-required)}"
        )
    for path in root.iterdir():
        mode = path.lstat().st_mode
        if path.name == "runtime":
            if not stat.S_ISDIR(mode) or path.is_symlink():
                raise ArtifactError("artifact runtime root invalid")
        elif not stat.S_ISREG(mode) or path.is_symlink():
            raise ArtifactError(f"artifact package member invalid: {path.name}")

    payload_raw, payload = _document(
        root / "payload-manifest.json", kind="aiwerk-payload-manifest"
    )
    aux_raw, aux = _document(root / "aux-manifest.json", kind="aiwerk-aux-manifest")
    release_raw, release = _document(root / "release.json", kind="aiwerk-release")
    publication_raw, publication = _document(
        root / "publication.json", kind="aiwerk-publication"
    )
    pin_raw, pin = _document(root / "approved-pin.json", kind="aiwerk-approved-pin")
    updater_raw, _updater = _document(
        root / "updater-manifest.json", kind="aiwerk-updater-source-manifest"
    )
    qualification_raw, qualification = _document(
        root / "qualification-receipt.json", kind="aiwerk-qualification-receipt"
    )
    detector_documents = [
        _document(root / f"detector-{index}.json", kind="aiwerk-detector-receipt")
        for index in (1, 2)
    ]
    payload_entries = _manifest_entries(payload, label="payload")
    aux_entries = _manifest_entries(aux, label="aux")
    payload_paths = {row["path"] for row in payload_entries}
    aux_paths = {row["path"] for row in aux_entries}
    if payload_paths & aux_paths:
        raise ArtifactError("payload and aux manifest paths overlap")
    if _inventory(root / "runtime") != payload_entries:
        raise ArtifactError("artifact runtime inventory mismatch")

    source_inventory = release.get("source_tree")
    if (
        release.get("release_id") != expected_commit
        or release.get("source_commit") != expected_commit
        or not isinstance(source_inventory, str)
        or len(source_inventory) != 64
        or release.get("migration_class") != "forward_only"
        or release.get("entry_module") != "hermes_cli.main"
    ):
        raise ArtifactError("artifact source or release identity mismatch")
    bindings = {
        "payload_manifest_sha256": _sha_bytes(payload_raw),
        "aux_manifest_sha256": _sha_bytes(aux_raw),
        "updater_manifest_sha256": _sha_bytes(updater_raw),
        "qualification_receipt_sha256": _sha_bytes(qualification_raw),
        "detector_receipt_sha256": [
            _sha_bytes(raw) for raw, _value in detector_documents
        ],
    }
    if any(release.get(key) != value for key, value in bindings.items()):
        raise ArtifactError("release manifest hash graph mismatch")
    if release.get("inventory_sha256") != _sha_bytes(canonical_bytes(payload_entries)):
        raise ArtifactError("release inventory digest mismatch")
    if (
        qualification.get("status") != "PASS"
        or qualification.get("source_commit") != expected_commit
        or qualification.get("source_tree") != source_inventory
    ):
        raise ArtifactError("qualification receipt mismatch")
    detector_values = [value for _raw, value in detector_documents]
    if (
        len({value.get("detector") for value in detector_values}) != 2
        or any(
            value.get("status") != "PASS"
            or value.get("source_commit") != expected_commit
            or value.get("source_tree") != source_inventory
            for value in detector_values
        )
    ):
        raise ArtifactError("detector receipts mismatch")

    archive = root / "artifact.tar"
    archive_hash = _sha_file(archive)
    archive_size = archive.stat().st_size
    publication_bindings = {
        "release_id": expected_commit,
        "release_manifest_sha256": _sha_bytes(release_raw),
        "archive_sha256": archive_hash,
        "archive_size": archive_size,
        "payload_manifest_sha256": _sha_bytes(payload_raw),
        "aux_manifest_sha256": _sha_bytes(aux_raw),
        "updater_manifest_sha256": _sha_bytes(updater_raw),
        "rollback_authority_sha256": None,
    }
    if any(publication.get(key) != value for key, value in publication_bindings.items()):
        raise ArtifactError("publication/archive hash graph mismatch")
    if (
        pin.get("release_id") != expected_commit
        or pin.get("publication_sha256") != _sha_bytes(publication_raw)
        or pin.get("updater_manifest_sha256") != _sha_bytes(updater_raw)
    ):
        raise ArtifactError("approved pin hash graph mismatch")
    _verify_archive(archive, [*payload_entries, *aux_entries])
    return {
        "schema_version": 1,
        "kind": "AIWERK_IMMUTABLE_ARTIFACT_VERIFICATION",
        "verdict": "PASS",
        "artifact_root": str(root),
        "source_commit": expected_commit,
        "source_git_tree": expected_git_tree,
        "source_inventory_sha256": source_inventory,
        "release_id": expected_commit,
        "release_manifest_sha256": _sha_bytes(release_raw),
        "archive_sha256": archive_hash,
        "archive_size": archive_size,
        "payload_manifest_sha256": _sha_bytes(payload_raw),
        "aux_manifest_sha256": _sha_bytes(aux_raw),
        "approved_pin_sha256": _sha_bytes(pin_raw),
    }
