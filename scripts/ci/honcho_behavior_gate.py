#!/usr/bin/env python3
"""Trusted-base Honcho gate. Never import/install candidate code on the host."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
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
        if sum(m.size for m in members) > 512 * 1024 * 1024:
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
        "--log-driver=none", "--tmpfs=/tmp:rw,nosuid,nodev,size=512m,mode=1777",
        "--mount", f"type=bind,src={source},dst=/candidate,readonly",
        "--mount", f"type=bind,src={trusted},dst=/trusted,readonly",
        "--mount", f"type=bind,src={results},dst=/results",
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


def run(repo, revision, image, evidence):
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("candidate must be an exact commit SHA")
    repo = repo.resolve()
    resolved = subprocess.check_output(["git", "-C", str(repo), "rev-parse", revision + "^{commit}"], text=True).strip()
    if resolved != revision:
        raise ValueError("candidate identity mismatch")
    image_id = subprocess.check_output(["docker", "image", "inspect", image, "--format", "{{.Id}}"], text=True).strip()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise ValueError("unresolved dependency image")
    trusted = Path(__file__).resolve().parents[2] / ".ci/honcho-behavior"
    harness_hash = hashlib.sha256((trusted / "test_wire.py").read_bytes()).hexdigest()
    archive = subprocess.check_output(["git", "-C", str(repo), "archive", "--format=tar", revision])
    evidence.mkdir(parents=True, exist_ok=True)
    receipt = {"commit": revision, "tree": subprocess.check_output(["git", "-C", str(repo), "rev-parse", revision + "^{tree}"], text=True).strip(),
               "harness_sha256": harness_hash, "image": image_id, "passed": False}
    with tempfile.TemporaryDirectory(prefix="honcho-gate-") as scratch:
        scratch = Path(scratch)
        scratch.chmod(0o755)
        source, results = scratch / "source", scratch / "results"
        extract_source(archive, source)
        results.mkdir(mode=0o777)
        results.chmod(0o777)
        name = "honcho-gate-" + uuid.uuid4().hex
        command = container_command(image_id, source, trusted, results, name)
        receipt["command"] = command
        try:
            completed = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300)
            receipt["returncode"] = completed.returncode
            try:
                report = read_report(results / "report.xml")
                (evidence / "report.xml").write_bytes(report)
                receipt["report_sha256"] = hashlib.sha256(report).hexdigest()
                validate_results(results / "report.xml", completed.returncode)
                receipt["passed"] = True
            except (ValueError, OSError) as exc:
                receipt["error"] = type(exc).__name__
        except subprocess.TimeoutExpired:
            receipt["error"] = "container timeout"
        finally:
            subprocess.run(["docker", "rm", "--force", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
            (evidence / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    # Never echo candidate stdout, XML, filenames or exception text as CI commands.
    print("Honcho retained behavior: " + ("PASS (2/2)" if receipt["passed"] else "FAIL"))
    return 0 if receipt["passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(run(args.repo, args.candidate, args.image, args.evidence.resolve()))
