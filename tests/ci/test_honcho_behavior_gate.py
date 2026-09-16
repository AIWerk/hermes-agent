"""Trusted gate rejects unsafe sources and non-completion (no Docker required)."""
import importlib.util
import io
import tarfile
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "honcho_gate", Path(__file__).resolve().parents[2] / "scripts/ci/honcho_behavior_gate.py"
)


def load_gate():
    module = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(module)
    return module


def test_container_contract(tmp_path):
    gate = load_gate()
    command = gate.container_command("sha256:" + "1" * 64, tmp_path / "source", tmp_path / "trusted", tmp_path / "results", "qualification")
    for flag in ("--network=none", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--user=65534:65534"):
        assert flag in command
    assert not any("docker.sock" in value or "--privileged" in value for value in command)
    assert "PYTEST_ADDOPTS" not in " ".join(command)
    assert "/trusted/test_wire.py" in command[-1]
    assert "--noconftest" in command[-1]
    assert "-I" in command


def test_archive_rejects_nonregular_members(tmp_path):
    gate = load_gate()
    for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE):
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w") as archive:
            member = tarfile.TarInfo("escape")
            member.type = kind
            member.linkname = "/etc/passwd"
            archive.addfile(member)
        with pytest.raises(ValueError):
            gate.extract_source(data.getvalue(), tmp_path / "out")
    for name in ("../escape", "/escape", ".git/config", "a/../../escape"):
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w") as archive:
            member = tarfile.TarInfo(name)
            archive.addfile(member, io.BytesIO())
        with pytest.raises(ValueError):
            gate.extract_source(data.getvalue(), tmp_path / "out")


def test_missing_or_incomplete_results_fail_closed(tmp_path):
    gate = load_gate()
    report = tmp_path / "report.xml"
    with pytest.raises((ValueError, OSError)):
        gate.validate_results(report, 0)
    valid = '<testsuites><testsuite tests="2" failures="0" errors="0" skipped="0">' + ''.join(
        f'<testcase classname="test_wire" name="test_honcho_section_policy_reaches_final_user_content[{case}]"/>'
        for case in ("root", "host")
    ) + '</testsuite></testsuites>'
    report.write_text(valid)
    gate.validate_results(report, 0)
    for xml, rc in ((valid, 1), (valid.replace('tests="2"', 'tests="0"'), 0),
                    (valid.replace('/>', '><skipped/></testcase>', 1), 0),
                    (valid.replace('[host]', '[root]'), 0), ('<broken', 0),
                    ('<testsuites/>', 0)):
        report.write_text(xml)
        with pytest.raises(ValueError):
            gate.validate_results(report, rc)
    report.unlink()
    report.symlink_to(tmp_path / "missing")
    with pytest.raises((ValueError, OSError)):
        gate.validate_results(report, 0)


def test_effective_merge_binds_both_sides_and_rejects_conflicts(tmp_path):
    import subprocess
    def git(*args):
        return subprocess.check_output(["git", "-C", str(tmp_path), *args], text=True).strip()
    git("init", "-q")
    git("config", "user.name", "Gate fixture")
    git("config", "user.email", "gate@example.invalid")
    def commit(name, content):
        (tmp_path / name).write_text(content)
        git("add", name); git("commit", "-qm", name)
        return git("rev-parse", "HEAD")
    origin = commit("policy", "good\n")
    base = commit("base-feature", "required\n")
    git("checkout", "-q", "--detach", origin)
    head = commit("head-feature", "candidate\n")
    gate = load_gate()
    assert hasattr(gate, "resolve_source"), "head-only gate lacks effective merge qualification"
    identity = gate.resolve_source(tmp_path, base, head)
    assert identity["base_commit"] == base and identity["head_commit"] == head
    assert identity["base_tree"] == git("rev-parse", base + "^{tree}")
    assert identity["head_tree"] == git("rev-parse", head + "^{tree}")
    tree = identity["effective_tree"]
    assert git("show", tree + ":base-feature") == "required"
    assert git("show", tree + ":head-feature") == "candidate"
    left = commit("policy", "left\n")
    git("checkout", "-q", "--detach", base)
    right = commit("policy", "right\n")
    with pytest.raises(ValueError):
        gate.resolve_source(tmp_path, right, left)
    with pytest.raises(ValueError):
        gate.resolve_source(tmp_path, "HEAD", head)


def test_report_storage_has_no_host_writer_and_bounds_collection(tmp_path):
    import sys
    gate = load_gate()
    command = gate.container_command("image", tmp_path / "s", tmp_path / "t", tmp_path / "r", "test")
    mounts = [command[i + 1] for i, value in enumerate(command) if value == "--mount"]
    assert all(m.endswith(",readonly") for m in mounts), "candidate has an unbounded host-writable mount"
    results = next(value for value in command if value.startswith("--tmpfs=/results:"))
    assert "size=" in results and "nr_inodes=" in results
    assert "--log-driver=none" in command
    assert gate.bounded_output([sys.executable, "-c", "print('ok')"], 16, 5) == b"ok\n"
    with pytest.raises(ValueError):
        gate.bounded_output([sys.executable, "-c", "import os; os.write(1,b'x'*1000000)"], 16, 5)
    with pytest.raises(Exception):
        gate.bounded_output([sys.executable, "-c", "import time; time.sleep(5)"], 16, 0.1)


@pytest.mark.parametrize("cleanup", ["timeout", "oserror", "nonzero", "success"])
def test_cleanup_outcome_controls_gate_result(tmp_path, monkeypatch, capsys, cleanup):
    import json
    import subprocess

    gate = load_gate()
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run([
        "git", "-C", str(repo), "-c", "user.name=Gate fixture",
        "-c", "user.email=gate@example.invalid", "commit", "--allow-empty", "-qm", "fixture",
    ], check=True)
    revision = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    report = ('<testsuites><testsuite tests="2" failures="0" errors="0" skipped="0">' + ''.join(
        f'<testcase classname="test_wire" name="{name}"/>' for name in sorted(gate.EXPECTED)
    ) + '</testsuite></testsuites>').encode()
    events = []
    original_output = gate.bounded_output
    original_run = subprocess.run
    original_validate = gate.validate_results

    def output(command, limit, timeout):
        if command[:3] == ["docker", "image", "inspect"]:
            return ("sha256:" + "1" * 64).encode()
        if command[:2] == ["docker", "run"]:
            return b"container-id"
        return original_output(command, limit, timeout)

    def run(command, **kwargs):
        if command[:2] == ["docker", "exec"]:
            events.append("behavior-completed")
            return subprocess.CompletedProcess(command, 0)
        if command[:3] == ["docker", "rm", "--force"]:
            assert events == ["behavior-completed", "report-validated"]
            events.append("cleanup")
            if cleanup == "timeout":
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            if cleanup == "oserror":
                raise OSError("injected cleanup failure")
            result = subprocess.CompletedProcess(command, 1 if cleanup == "nonzero" else 0)
            if kwargs.get("check"):
                result.check_returncode()
            return result
        return original_run(command, **kwargs)

    def validate(path, returncode, **kwargs):
        original_validate(path, returncode, **kwargs)
        events.append("report-validated")

    monkeypatch.setattr(gate, "bounded_output", output)
    monkeypatch.setattr(gate.subprocess, "run", run)
    monkeypatch.setattr(gate, "collect_report", lambda name: report)
    monkeypatch.setattr(gate, "validate_results", validate)
    evidence = tmp_path / "evidence"
    status = gate.run(repo, revision, "fixture-image", evidence, revision)
    receipt = json.loads((evidence / "receipt.json").read_text())
    assert events == ["behavior-completed", "report-validated", "cleanup"]
    assert receipt["returncode"] == 0
    assert (evidence / "report.xml").read_bytes() == report
    success = cleanup == "success"
    assert (receipt["passed"], status) == (success, 0 if success else 1), receipt
    assert capsys.readouterr().out == "Honcho retained behavior: " + ("PASS (2/2)\n" if success else "FAIL\n")
    if success:
        assert "error" not in receipt
    else:
        assert receipt["error"] == {
            "timeout": "TimeoutExpired", "oserror": "OSError", "nonzero": "CalledProcessError",
        }[cleanup]


@pytest.mark.parametrize("suite,harness,count,label", [
    ("honcho", "test_wire.py", 2, "Honcho"),
    ("session-history", "test_history.py", 198, "Session-history"),
])
def test_fixed_profiles_and_cli(tmp_path, suite, harness, count, label):
    import subprocess
    import sys
    from collections import Counter
    gate = load_gate()
    profile = gate.suite_profile(suite)
    assert profile.harness.name == harness
    assert profile.label == label
    assert len(profile.expected) == count
    assert profile.classname == harness[:-3]
    command = gate.container_command("image", tmp_path / "s", tmp_path / "t", None, "test", suite=suite)
    assert "/trusted/" + harness in command[-1]
    assert "--network=none" in command and "--noconftest" in command[-1]
    if suite == "session-history":
        assert Counter(n.split("[")[0] for n in profile.expected) == {
            "test_exact_owner_recents_page_into_readable_history": 40,
            "test_recents_and_history_deny_unproven_or_conflicting_ownership": 40,
            "test_http_recent_id_resumes_through_authenticated_rpc_with_history": 56,
            "test_new_legacy_resume_acceptance_does_not_admit_invalid_owners": 60,
            "test_admin_history_cannot_launder_foreign_compression_lineage": 2,
        }
    assert SPEC is not None
    help_result = subprocess.run([sys.executable, str(SPEC.origin), "--suite", suite, "--help"], capture_output=True, text=True)
    assert help_result.returncode == 0
    assert "--suite {honcho,session-history}" in help_result.stdout
    rejected = subprocess.run([sys.executable, str(SPEC.origin), "--suite", "candidate-path"], capture_output=True, text=True)
    assert rejected.returncode == 2
    assert "invalid choice" in rejected.stderr


def profile_xml(profile):
    import xml.etree.ElementTree as ET
    root = ET.Element("testsuites")
    suite = ET.SubElement(root, "testsuite", tests=str(len(profile.expected)), failures="0", errors="0", skipped="0")
    for name in sorted(profile.expected):
        ET.SubElement(suite, "testcase", classname=profile.classname, name=name)
    return root


@pytest.mark.parametrize("suite", ["honcho", "session-history"])
@pytest.mark.parametrize("fault", ["valid", "missing", "duplicate", "foreign", "classname", "skip", "error", "failure", "count", "nonzero", "malformed", "absent", "wrong-profile"])
def test_profile_result_validation(tmp_path, suite, fault):
    import xml.etree.ElementTree as ET
    gate = load_gate()
    profile = gate.suite_profile(suite)
    root = profile_xml(profile)
    testsuite = root[0]
    if fault == "missing": testsuite.remove(testsuite[0])
    if fault == "duplicate": testsuite[0].set("name", testsuite[1].get("name"))
    if fault == "foreign": testsuite[0].set("name", "test_candidate_invented")
    if fault == "classname": testsuite[0].set("classname", "test_candidate")
    if fault in ("skip", "error", "failure"):
        ET.SubElement(testsuite[0], {"skip": "skipped", "error": "error", "failure": "failure"}[fault])
    if fault == "count": testsuite.set("tests", "0")
    path = tmp_path / "report.xml"
    if fault != "absent": path.write_bytes(b"<broken" if fault == "malformed" else ET.tostring(root))
    selected = ("session-history" if suite == "honcho" else "honcho") if fault == "wrong-profile" else suite
    if fault == "valid":
        gate.validate_results(path, 0, suite=selected)
    else:
        with pytest.raises((ValueError, OSError)):
            gate.validate_results(path, 1 if fault == "nonzero" else 0, suite=selected)


def test_unknown_suite_rejected_before_execution(tmp_path, monkeypatch):
    gate = load_gate()
    with pytest.raises(ValueError): gate.suite_profile("../../candidate")
    with pytest.raises(ValueError): gate.container_command("i", tmp_path, tmp_path, None, "n", suite="unknown")
    monkeypatch.setattr(gate, "resolve_source", lambda *a: pytest.fail("unknown suite executed Git"))
    assert gate.run(tmp_path, "head", "image", tmp_path / "e", "base", suite="unknown") == 1


@pytest.mark.parametrize("suite", ["honcho", "session-history"])
@pytest.mark.parametrize("missing", [False, True])
def test_profile_authority_hashes_and_receipt(tmp_path, monkeypatch, capsys, suite, missing):
    import hashlib
    import json
    import subprocess
    import xml.etree.ElementTree as ET
    gate = load_gate()
    authority = tmp_path / "authority"
    launcher = authority / "scripts/ci/honcho_behavior_gate.py"
    monkeypatch.setattr(gate, "__file__", str(launcher))
    harness = ".ci/honcho-behavior/test_wire.py" if suite == "honcho" else ".ci/session-history-behavior/test_history.py"
    paths = ["scripts/ci/honcho_behavior_gate.py", harness, ".ci/honcho-behavior/Dockerfile", "uv.lock", "pyproject.toml"]
    for path in paths:
        target = authority / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("trusted:" + path)
    if missing: (authority / harness).unlink()
    monkeypatch.setattr(gate, "resolve_source", lambda *a: {"effective_tree": "a" * 40})
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w"): pass
    commands = []
    def output(command, limit, timeout):
        commands.append(command)
        if command[:3] == ["docker", "image", "inspect"]: return ("sha256:" + "1" * 64).encode()
        if "archive" in command: return archive.getvalue()
        return b"a" * 40
    monkeypatch.setattr(gate, "bounded_output", output)
    monkeypatch.setattr(gate.subprocess, "run", lambda command, **kw: subprocess.CompletedProcess(command, 0))
    monkeypatch.setattr(gate, "collect_report", lambda name: ET.tostring(profile_xml(gate.suite_profile(suite))))
    evidence = tmp_path / "evidence"
    assert gate.run(tmp_path, "head", "image", evidence, "base", suite=suite) == int(missing)
    receipt = json.loads((evidence / "receipt.json").read_text())
    assert receipt["suite"] == suite
    if missing:
        assert not receipt["passed"]
        assert not any(c[:2] == ["docker", "run"] for c in commands)
    else:
        assert receipt["authority_sha256"] == {p: hashlib.sha256((authority / p).read_bytes()).hexdigest() for p in paths}
        assert str((authority / harness).parent) in " ".join(receipt["container_command"])
        assert f"PASS ({len(gate.suite_profile(suite).expected)}/{len(gate.suite_profile(suite).expected)})" in capsys.readouterr().out
