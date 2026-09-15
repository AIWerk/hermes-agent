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
