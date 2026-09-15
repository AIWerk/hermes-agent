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
