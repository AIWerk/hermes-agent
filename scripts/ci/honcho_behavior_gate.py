#!/usr/bin/env python3
"""Trusted-base Honcho gate. Never import/install candidate code on the host."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import selectors
import time
import stat
import subprocess
import tarfile
import tempfile
import uuid
import xml.etree.ElementTree as ET

EXPECTED = {
    f"test_honcho_section_policy_reaches_final_user_content[{case}]"
    for case in ("root", "host")
}


def extract_source(data, destination):
    """Extract Git data, not links/devices or repository metadata."""
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        members = archive.getmembers()
        if len(members) > 100000 or sum(m.size for m in members) > 512 * 1024 * 1024:
            raise ValueError("source exceeds budget")
        for member in members:
            path = PurePosixPath(member.name)
            if (path.is_absolute() or ".." in path.parts or ".git" in path.parts
                    or not path.parts or not (member.isfile() or member.isdir())):
                raise ValueError("unsafe source member")
        destination.mkdir(parents=True, exist_ok=True)
        for member in members:
            target = destination / member.name
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as source, target.open("xb") as out:
                    out.write(source.read())
                target.chmod(0o755 if member.mode & 0o111 else 0o644)


def container_command(image, source, trusted, results, name):
    # Import trusted pytest before exposing candidate imports. No candidate tests,
    # conftest, pytest config, plugins or packaging hooks participate in selection.
    launcher = (
        "import sys,pytest; sys.path.insert(0,'/candidate'); "
        "raise SystemExit(pytest.main(['-c','/dev/null','--noconftest',"
        "'--rootdir=/trusted','--confcutdir=/trusted','--import-mode=importlib','-p','no:cacheprovider',"
        "'-v','--tb=short','--junitxml=/results/report.xml','/trusted/test_wire.py']))"
    )
    return [
        "docker", "run", "--rm", "--name", name, "--network=none", "--read-only",
        "--cap-drop=ALL", "--security-opt=no-new-privileges", "--user=65534:65534",
        "--pids-limit=256", "--memory=2g", "--memory-swap=2g", "--cpus=2",
        "--log-driver=none", "--tmpfs=/tmp:rw,nosuid,nodev,size=512m,nr_inodes=16384,mode=1777",
        "--tmpfs=/results:rw,nosuid,nodev,noexec,size=4m,nr_inodes=64,mode=1777",
        "--mount", f"type=bind,src={source},dst=/candidate,readonly",
        "--mount", f"type=bind,src={trusted},dst=/trusted,readonly",
        "--workdir=/candidate", "--entrypoint=/usr/local/bin/python",
        "--env=HOME=/tmp/home", "--env=HERMES_HOME=/tmp/home/.hermes",
        "--env=PYTEST_DISABLE_PLUGIN_AUTOLOAD=1", "--env=PYTHONDONTWRITEBYTECODE=1",
        "--env=HERMES_DISABLE_LAZY_INSTALLS=1", "--env=TIRITH_ENABLED=false",
        image, "-I", "-c", launcher,
    ]


def read_report(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 2 * 1024 * 1024:
            raise ValueError("invalid report type or size")
        return stream.read(2 * 1024 * 1024 + 1)


def validate_results(path, returncode):
    if returncode != 0:
        raise ValueError("candidate process did not succeed")
    try:
        root = ET.fromstring(read_report(path))
        suites = list(root.iter("testsuite"))
        cases = list(root.iter("testcase"))
        if len(suites) != 1 or int(suites[0].get("tests", "-1")) != 2:
            raise ValueError("wrong test count")
        if any(int(suites[0].get(key, "-1")) != 0 for key in ("failures", "errors", "skipped")):
            raise ValueError("nonpassing suite")
        if len(cases) != 2 or {c.get("name") for c in cases} != EXPECTED:
            raise ValueError("missing, replaced or duplicate node")
        if any(list(case) or case.get("classname") != "test_wire" for case in cases):
            raise ValueError("nonpassing or foreign node")
    except (ET.ParseError, TypeError) as exc:
        raise ValueError("invalid report") from exc


def bounded_output(command, limit, timeout):
    """Bound pipe bytes and elapsed time; never log candidate output."""
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as proc:
        try:
            deadline = time.monotonic() + timeout
            data = bytearray()
            with selectors.DefaultSelector() as selector:
                selector.register(proc.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise subprocess.TimeoutExpired(command, timeout)
                    chunk = os.read(proc.stdout.fileno(), min(65536, limit + 1 - len(data)))
                    if not chunk:
                        break
                    data.extend(chunk)
                    if len(data) > limit:
                        raise ValueError("output exceeds budget")
            if proc.wait(timeout=max(0.01, deadline - time.monotonic())) != 0:
                raise ValueError("bounded command failed")
            return bytes(data)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()


def resolve_source(repo, base, head):
    """Construct the exact effective tree, without candidate checkout or hooks."""
    git = ["git", "--no-replace-objects", "-C", str(repo)]
    identity = {}
    for role, revision in (("base", base), ("head", head)):
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ValueError("source must be an exact commit SHA")
        resolved = bounded_output(git + ["rev-parse", revision + "^{commit}"], 128, 30).decode().strip()
        if resolved != revision:
            raise ValueError("source identity mismatch")
        identity[role + "_commit"] = revision
        identity[role + "_tree"] = bounded_output(git + ["rev-parse", revision + "^{tree}"], 128, 30).decode().strip()
    # --no-messages keeps conflict paths out of host logs. Nonzero exit rejects
    # conflicts (including a tree Git produced containing conflict markers).
    tree = bounded_output(git + ["merge-tree", "--write-tree", "--no-messages", base, head], 128, 60).decode().strip()
    if not re.fullmatch(r"[0-9a-f]{40}", tree):
        raise ValueError("invalid effective tree")
    identity["effective_tree"] = tree
    identity["merge_strategy"] = "git merge-tree --write-tree"
    identity["git_version"] = bounded_output(git + ["--version"], 128, 10).decode().strip()
    return identity


def collect_report(name):
    # Separate trusted interpreter: no candidate imports. The host additionally
    # bounds stdout regardless of the container-side checks; no recursive copy.
    reader = (
        "import os,stat,sys; "
        "fd=os.open('/results/report.xml',os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK); "
        "s=os.fstat(fd); "
        "assert stat.S_ISREG(s.st_mode) and s.st_size<=2097152; "
        "data=os.read(fd,2097153); assert len(data)<=2097152; "
        "sys.stdout.buffer.write(data)"
    )
    return bounded_output(["docker", "exec", name, "/usr/local/bin/python", "-I", "-c", reader], 2097152, 30)


def run(repo, revision, image, evidence, base):
    repo = repo.resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    receipt = {"passed": False}
    try:
        receipt.update(resolve_source(repo, base, revision))
        image_id = bounded_output(["docker", "image", "inspect", image, "--format", "{{.Id}}"], 128, 30).decode().strip()
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
            raise ValueError("unresolved dependency image")
        authority = Path(__file__).resolve().parents[2]
        trusted = authority / ".ci/honcho-behavior"
        receipt.update({
            "image": image_id,
            "authority_commit": bounded_output(["git", "-C", str(authority), "rev-parse", "HEAD"], 128, 30).decode().strip(),
            "authority_tree": bounded_output(["git", "-C", str(authority), "rev-parse", "HEAD^{tree}"], 128, 30).decode().strip(),
            "authority_sha256": {path: hashlib.sha256((authority / path).read_bytes()).hexdigest()
                for path in ("scripts/ci/honcho_behavior_gate.py", ".ci/honcho-behavior/test_wire.py",
                             ".ci/honcho-behavior/Dockerfile", "uv.lock", "pyproject.toml")},
        })
        archive = bounded_output(["git", "--no-replace-objects", "-C", str(repo), "archive", "--format=tar", receipt["effective_tree"]], 600 * 1024 * 1024, 60)
        with tempfile.TemporaryDirectory(prefix="honcho-gate-") as scratch:
            scratch = Path(scratch)
            scratch.chmod(0o755)
            source = scratch / "source"
            extract_source(archive, source)
            name = "honcho-gate-" + uuid.uuid4().hex
            command = container_command(image_id, source, trusted, None, name)
            # Keep tmpfs mounted while the test exec ends and the bounded report
            # is collected. Idle PID1 is trusted, not the candidate interpreter.
            start = command[:-3] + ["-I", "-c", "import time; time.sleep(390)"]
            start.insert(2, "--detach")
            execute = ["docker", "exec", name, "/usr/local/bin/python"] + command[-3:]
            receipt["command"] = execute
            receipt["container_command"] = start
            try:
                bounded_output(start, 128, 30)
                completed = subprocess.run(execute, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300)
                receipt["returncode"] = completed.returncode
                report = collect_report(name)
                (evidence / "report.xml").write_bytes(report)
                receipt["report_sha256"] = hashlib.sha256(report).hexdigest()
                validate_results(evidence / "report.xml", completed.returncode)
            finally:
                subprocess.run(["docker", "rm", "--force", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=True)
        receipt["passed"] = True
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        receipt["error"] = type(exc).__name__
    finally:
        (evidence / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding='utf-8')
    print("Honcho retained behavior: " + ("PASS (2/2)" if receipt["passed"] else "FAIL"))
    return 0 if receipt["passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(run(args.repo, args.candidate, args.image, args.evidence.resolve(), args.base))
