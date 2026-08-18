from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parent.parent
_COLLECTION_LAZY_INSTALL_SEAL = os.environ.get("HERMES_DISABLE_LAZY_INSTALLS")


def test_test_session_seals_lazy_installs_before_installer(monkeypatch):
    """Tests must never mutate their locked interpreter by installing packages."""
    from tools import lazy_deps

    assert _COLLECTION_LAZY_INSTALL_SEAL == "1"
    monkeypatch.delenv("HERMES_LAZY_INSTALL_TARGET", raising=False)
    monkeypatch.setattr(
        lazy_deps,
        "feature_missing",
        lambda _feature: ("boto3==1.42.89",),
    )

    installer_called = False

    def _forbidden_installer(_specs, *, timeout=300):
        nonlocal installer_called
        installer_called = True
        raise AssertionError("test environment reached the lazy installer")

    monkeypatch.setattr(lazy_deps, "_venv_pip_install", _forbidden_installer)

    with pytest.raises(lazy_deps.FeatureUnavailable, match="lazy installs disabled"):
        lazy_deps.ensure("provider.bedrock", prompt=False)

    assert installer_called is False


def test_gate_e_known_fixed_shared_tmp_writers_are_removed():
    """Known Gate-E leaks must use pytest-owned roots, not global names."""
    cases = {
        "tests/gateway/test_multiplex_adapter_registry.py": {
            "/tmp/x",
            "/tmp/y",
            "/tmp/default",
            "/tmp/bad",
            "/tmp/good",
            "/tmp/unsafe",
        },
        "tests/secret_sources/test_profile_secrets.py": {"/tmp/x/.hermes"},
        "tests/test_tui_gateway_server.py": {"/tmp/test-profile"},
    }
    problems = []
    for relative, forbidden in cases.items():
        path = _REPO_ROOT / relative
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value in forbidden
        }
        if found:
            problems.append(f"{relative}: {sorted(found)}")

    assert not problems, "fixed shared-/tmp writers remain: " + "; ".join(problems)
