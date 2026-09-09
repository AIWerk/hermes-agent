from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "scripts" / "ci" / "run_refresh_diagnostic.py"
RUNBOOK = ROOT / "docs" / "aiwerk-upstream-refresh-runbook.md"


def test_refresh_diagnostic_runner_isolates_sessiondb_from_caller_home(tmp_path: Path) -> None:
    caller_home = tmp_path / "caller-home"
    live_hermes_home = caller_home / ".hermes"
    live_hermes_home.mkdir(parents=True)
    sentinel = live_hermes_home / "must-not-change"
    sentinel.write_text("original", encoding="utf-8")

    child = (
        "import json, os; "
        "from hermes_state import SessionDB; "
        "db=SessionDB(); "
        "print(json.dumps({'home': os.environ['HOME'], "
        "'hermes_home': os.environ['HERMES_HOME'], "
        "'tmpdir': os.environ['TMPDIR'], 'db_path': str(db.db_path)})); "
        "db.close()"
    )
    env = dict(os.environ)
    env.update({
        "HOME": str(caller_home),
        "HERMES_HOME": str(live_hermes_home),
        "TMPDIR": str(tmp_path / "caller-tmp"),
    })
    result = subprocess.run(
        [sys.executable, str(RUNNER), "--", sys.executable, "-c", child],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert Path(payload["hermes_home"]) != live_hermes_home
    assert Path(payload["db_path"]) == Path(payload["hermes_home"]) / "state.db"
    isolated_root = Path(payload["hermes_home"]).parent.parent
    assert Path(payload["home"]).parent == isolated_root
    assert Path(payload["tmpdir"]).parent == isolated_root
    assert sentinel.read_text(encoding="utf-8") == "original"
    assert not (live_hermes_home / "state.db").exists()


def test_refresh_diagnostic_tmpdir_supports_af_unix_socket_paths(tmp_path: Path) -> None:
    child = (
        "import json, os, socket; "
        "path = os.path.join(os.environ['TMPDIR'], 'read-special-file-guard.sock'); "
        "sock = socket.socket(socket.AF_UNIX); "
        "sock.bind(path); "
        "sock.close(); "
        "print(json.dumps({'tmpdir': os.environ['TMPDIR'], 'socket_path': path, 'limit': 107}))"
    )
    result = subprocess.run(
        [sys.executable, str(RUNNER), "--", sys.executable, "-c", child],
        cwd=ROOT,
        env={**os.environ, "HOME": str(tmp_path / "home"), "TMPDIR": str(tmp_path / "tmp")},
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert Path(payload["tmpdir"]).is_relative_to("/tmp")
    assert len(payload["socket_path"].encode()) <= payload["limit"]


def test_refresh_diagnostic_scrubs_secret_env_and_hides_live_home(tmp_path: Path) -> None:
    live_home = tmp_path / "caller-home"
    live_hermes = live_home / ".hermes"
    live_hermes.mkdir(parents=True)
    secret_file = live_hermes / "secret-state"
    secret_file.write_text("live secret", encoding="utf-8")
    child = (
        "import json, os, pathlib; "
        f"live=pathlib.Path({str(live_hermes)!r}); "
        "print(json.dumps({'api_key': os.environ.get('OPENAI_API_KEY'), "
        "'injected': os.environ.get('INJECTED_LIVE_HERMES'), "
        "'can_read_live': live.joinpath('secret-state').exists()}))"
    )

    result = subprocess.run(
        [sys.executable, str(RUNNER), "--", sys.executable, "-c", child],
        cwd=ROOT,
        env={
            **os.environ,
            "HOME": str(live_home),
            "HERMES_HOME": str(live_hermes),
            "OPENAI_API_KEY": "sk-live-secret",
            "INJECTED_LIVE_HERMES": str(live_hermes),
        },
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload == {"api_key": None, "injected": None, "can_read_live": False}
    assert secret_file.read_text(encoding="utf-8") == "live secret"


def test_refresh_diagnostic_repo_is_read_only(tmp_path: Path) -> None:
    marker = ROOT / "refresh-diagnostic-must-not-write"
    child = (
        "import json, pathlib\n"
        f"path=pathlib.Path({str(marker)!r})\n"
        "try:\n"
        "    path.write_text('bad', encoding='utf-8')\n"
        "    wrote=True\n"
        "except OSError as exc:\n"
        "    wrote=False\n"
        "    err=type(exc).__name__\n"
        "print(json.dumps({'wrote': wrote, 'err': err}))"
    )

    result = subprocess.run(
        [sys.executable, str(RUNNER), "--", sys.executable, "-c", child],
        cwd=ROOT,
        env={**os.environ, "HOME": str(tmp_path / "home")},
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload == {"wrote": False, "err": "OSError"}
    assert not marker.exists()


def test_refresh_diagnostic_denies_network_egress(tmp_path: Path) -> None:
    child = (
        "import json, socket\n"
        "sock=socket.socket()\n"
        "sock.settimeout(1)\n"
        "try:\n"
        "    sock.connect(('1.1.1.1', 80))\n"
        "    connected=True\n"
        "except OSError as exc:\n"
        "    connected=False\n"
        "    err=type(exc).__name__\n"
        "print(json.dumps({'connected': connected, 'err': err}))"
    )

    result = subprocess.run(
        [sys.executable, str(RUNNER), "--", sys.executable, "-c", child],
        cwd=ROOT,
        env={**os.environ, "HOME": str(tmp_path / "home")},
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["connected"] is False


def test_refresh_diagnostic_can_write_isolated_home_tmp_and_run_pytest(tmp_path: Path) -> None:
    child = (
        "import os, pathlib, subprocess, sys; "
        "test_path=pathlib.Path('/tmp/work/test_smoke.py'); "
        "test_path.write_text('def test_smoke(tmp_path):\\n    assert tmp_path.exists()\\n', encoding='utf-8'); "
        "pathlib.Path(os.environ['HOME'], 'home-write').write_text('ok', encoding='utf-8'); "
        "pathlib.Path(os.environ['TMPDIR'], 'tmp-write').write_text('ok', encoding='utf-8'); "
        "raise SystemExit(subprocess.run([sys.executable, '-m', 'pytest', '-q', str(test_path)], check=False).returncode)"
    )
    result = subprocess.run(
        [sys.executable, str(RUNNER), "--", sys.executable, "-c", child],
        cwd=ROOT,
        env={**os.environ, "HOME": str(tmp_path / "home")},
        text=True,
        capture_output=True,
        timeout=90,
        check=False,
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert "1 passed" in result.stdout


def test_refresh_diagnostic_ignores_fake_path_bwrap(tmp_path: Path, monkeypatch) -> None:
    import scripts.ci.run_refresh_diagnostic as runner

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_bwrap = fake_bin / "bwrap"
    fake_bwrap.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    fake_bwrap.chmod(0o755)
    monkeypatch.setenv("PATH", os.fspath(fake_bin))

    command = runner._bubblewrap_command([sys.executable, "-c", "print('ok')"], repo_root=ROOT)

    assert command[0] == os.fspath(runner._TRUSTED_BWRAP)
    assert os.fspath(fake_bwrap) not in command


def test_refresh_diagnostic_fails_closed_without_bubblewrap(monkeypatch) -> None:
    import scripts.ci.run_refresh_diagnostic as runner

    monkeypatch.setattr(runner, "_TRUSTED_BWRAP", Path("/definitely/missing/bwrap"))

    assert runner.main(["--", sys.executable, "-c", "print('nope')"]) == 125


def test_refresh_diagnostic_rejects_symlink_bubblewrap(tmp_path: Path, monkeypatch) -> None:
    import scripts.ci.run_refresh_diagnostic as runner

    target = tmp_path / "target-bwrap"
    target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    target.chmod(0o755)
    symlink = tmp_path / "bwrap"
    symlink.symlink_to(target)
    monkeypatch.setattr(runner, "_TRUSTED_BWRAP", symlink)

    assert runner.main(["--", sys.executable, "-c", "print('nope')"]) == 125


def test_refresh_diagnostic_rejects_untrusted_bubblewrap_metadata(tmp_path: Path, monkeypatch) -> None:
    import scripts.ci.run_refresh_diagnostic as runner

    fake_bwrap = tmp_path / "bwrap"
    fake_bwrap.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_bwrap.chmod(0o755)
    monkeypatch.setattr(runner, "_TRUSTED_BWRAP", fake_bwrap)

    assert runner.main(["--", sys.executable, "-c", "print('nope')"]) == 125


def test_refresh_diagnostic_rejects_group_writable_bubblewrap_metadata(monkeypatch) -> None:
    import stat
    import types

    import scripts.ci.run_refresh_diagnostic as runner

    def fake_lstat(path: Path) -> types.SimpleNamespace:
        assert path == runner._TRUSTED_BWRAP
        return types.SimpleNamespace(st_mode=stat.S_IFREG | 0o775, st_uid=0)

    monkeypatch.setattr(os, "lstat", fake_lstat)

    try:
        runner._authenticate_bubblewrap()
    except RuntimeError as exc:
        assert "group/world-writable" in str(exc)
    else:
        raise AssertionError("group-writable bubblewrap metadata was accepted")


def test_refresh_diagnostic_does_not_bind_broad_etc_or_shared_uv_parent(monkeypatch) -> None:
    import scripts.ci.run_refresh_diagnostic as runner

    command = runner._bubblewrap_command([sys.executable, "-c", "print('ok')"], repo_root=ROOT)
    pairs = list(zip(command, command[1:], strict=False))
    prefix = Path(sys.base_prefix).resolve()

    assert ("--ro-bind", "/etc") not in pairs
    assert ("--ro-bind", "/usr") not in pairs
    assert ("--ro-bind", os.fspath(prefix.parent)) not in pairs
    assert ("--ro-bind", os.fspath(prefix)) in pairs


def test_refresh_diagnostic_hides_etc_sibling_host_files(tmp_path: Path) -> None:
    child = (
        "import json, pathlib; "
        "print(json.dumps({"
        "'hosts': pathlib.Path('/etc/hosts').exists(), "
        "'passwd': pathlib.Path('/etc/passwd').exists(), "
        "'group': pathlib.Path('/etc/group').exists(), "
        "'nsswitch': pathlib.Path('/etc/nsswitch.conf').exists()"
        "}))"
    )
    result = subprocess.run(
        [sys.executable, str(RUNNER), "--", sys.executable, "-c", child],
        cwd=ROOT,
        env={**os.environ, "HOME": str(tmp_path / "home")},
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload == {"hosts": False, "passwd": True, "group": True, "nsswitch": True}


def test_refresh_diagnostic_hides_sibling_uv_runtimes(tmp_path: Path) -> None:
    import scripts.ci.run_refresh_diagnostic as runner

    prefix = Path(sys.base_prefix).resolve()
    if "uv" not in prefix.parts:
        return
    allowed = sorted(
        candidate.name
        for candidate in runner._interpreter_prefixes(ROOT)
        if candidate.parent == prefix.parent
    )
    child = (
        "import json, pathlib, sys; "
        "prefix=pathlib.Path(sys.base_prefix).resolve(); "
        "shared=prefix.parent; "
        "print(json.dumps({'prefix': prefix.name, 'siblings': sorted(p.name for p in shared.iterdir())}))"
    )
    result = subprocess.run(
        [sys.executable, str(RUNNER), "--", sys.executable, "-c", child],
        cwd=ROOT,
        env={**os.environ, "HOME": str(tmp_path / "home")},
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["prefix"] in allowed
    assert payload["siblings"] == allowed


def test_refresh_runbook_requires_the_isolated_diagnostic_runner() -> None:
    assert os.access(RUNNER, os.X_OK)
    text = RUNBOOK.read_text(encoding="utf-8")
    assert "scripts/ci/run_refresh_diagnostic.py" in text
    assert "HERMES_HOME" in text
    assert "diagnostic" in text.lower()
