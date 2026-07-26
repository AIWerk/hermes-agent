"""Regression tests for the upstream lark-oapi pkg_resources warning."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import warnings

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = [
    pytest.mark.filterwarnings(
        "ignore:Deprecated call to `pkg_resources.declare_namespace.*:DeprecationWarning"
    ),
    pytest.mark.filterwarnings(
        "ignore:datetime.datetime.utcfromtimestamp.*:DeprecationWarning"
    ),
]


def test_lark_warning_filter_is_narrow() -> None:
    from plugins.platforms.feishu.adapter import _suppress_lark_pkg_resources_warning

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        with _suppress_lark_pkg_resources_warning():
            warnings.warn_explicit(
                "pkg_resources is deprecated as an API.",
                UserWarning,
                "lark_oapi/ws/pb/google/__init__.py",
                2,
                module="lark_oapi.ws.pb.google",
            )

        with pytest.raises(UserWarning, match="unrelated warning"):
            with _suppress_lark_pkg_resources_warning():
                warnings.warn("unrelated warning", UserWarning, stacklevel=1)


@pytest.mark.skipif(
    importlib.util.find_spec("lark_oapi") is None,
    reason="lark-oapi is an optional lazy dependency",
)
def test_feishu_adapter_import_has_no_pkg_resources_warning() -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    result = subprocess.run(
        [
            sys.executable,
            "-W",
            "error::UserWarning",
            "-c",
            "import plugins.platforms.feishu.adapter",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
