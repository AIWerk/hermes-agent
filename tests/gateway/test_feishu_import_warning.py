"""Regression tests for lark-oapi's optional-import warning."""

from __future__ import annotations

import builtins
import importlib.util
import sys
import warnings
from pathlib import Path

import pytest


_KNOWN_WARNING = "pkg_resources is deprecated as an API"


def _warn_from_lark(
    message: str = _KNOWN_WARNING,
    category: type[Warning] = UserWarning,
    module: str = "lark_oapi",
) -> None:
    warnings.warn_explicit(
        message,
        category,
        filename=f"{module.replace('.', '/')}/__init__.py",
        lineno=1,
        module=module,
    )


def _reset_lark_state(adapter, monkeypatch) -> None:
    monkeypatch.setattr(adapter, "FEISHU_AVAILABLE", False)
    monkeypatch.setattr(adapter, "lark", None)


def test_sdk_import_suppresses_known_pkg_resources_warning(monkeypatch) -> None:
    from plugins.platforms.feishu import adapter

    _reset_lark_state(adapter, monkeypatch)
    original_import = builtins.__import__

    def warning_import(name, *args, **kwargs):
        if name == "lark_oapi":
            _warn_from_lark()
            raise ImportError("synthetic missing SDK")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", warning_import)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert adapter._load_lark_oapi() is False


def test_sdk_import_does_not_suppress_unrelated_user_warning(monkeypatch) -> None:
    from plugins.platforms.feishu import adapter

    _reset_lark_state(adapter, monkeypatch)
    original_import = builtins.__import__

    def warning_import(name, *args, **kwargs):
        if name == "lark_oapi":
            _warn_from_lark("unrelated lark warning")
            raise AssertionError("warning-as-error must fire before this")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", warning_import)
    with warnings.catch_warnings(), pytest.raises(UserWarning, match="unrelated"):
        warnings.simplefilter("error")
        adapter._load_lark_oapi()


@pytest.mark.parametrize(
    ("category", "module"),
    [
        (DeprecationWarning, "lark_oapi"),
        (UserWarning, "unrelated_optional_package"),
    ],
)
def test_sdk_import_filter_matches_category_and_module_narrowly(
    monkeypatch, category: type[Warning], module: str
) -> None:
    from plugins.platforms.feishu import adapter

    _reset_lark_state(adapter, monkeypatch)
    original_import = builtins.__import__

    def warning_import(name, *args, **kwargs):
        if name == "lark_oapi":
            _warn_from_lark(category=category, module=module)
            raise AssertionError("warning-as-error must fire before this")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", warning_import)
    with warnings.catch_warnings(), pytest.raises(category):
        warnings.simplefilter("error")
        adapter._load_lark_oapi()


def test_requirement_check_suppresses_known_pkg_resources_warning(monkeypatch) -> None:
    from plugins.platforms.feishu import adapter
    from tools import lazy_deps

    _reset_lark_state(adapter, monkeypatch)

    def warning_ensure(feature: str, *, prompt: bool) -> None:
        assert (feature, prompt) == ("platform.feishu", False)
        _warn_from_lark()

    monkeypatch.setattr(lazy_deps, "ensure", warning_ensure)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert adapter.check_feishu_requirements() is True


def test_requirement_check_does_not_suppress_unrelated_user_warning(monkeypatch) -> None:
    from plugins.platforms.feishu import adapter
    from tools import lazy_deps

    _reset_lark_state(adapter, monkeypatch)

    def warning_ensure(_feature: str, *, prompt: bool) -> None:
        assert prompt is False
        _warn_from_lark("unrelated lazy dependency warning")

    monkeypatch.setattr(lazy_deps, "ensure", warning_ensure)
    with warnings.catch_warnings(), pytest.raises(UserWarning, match="unrelated"):
        warnings.simplefilter("error")
        adapter.check_feishu_requirements()


def test_feishu_test_availability_probe_does_not_import_lark_oapi(monkeypatch) -> None:
    """Collection must inspect the optional package without executing it."""
    source = Path(__file__).with_name("test_feishu.py").read_text(encoding="utf-8")
    attempted_imports: list[str] = []
    original_import = builtins.__import__

    def tracking_import(name, *args, **kwargs):
        if name == "lark_oapi" or name.startswith("lark_oapi."):
            attempted_imports.append(name)
            raise AssertionError("availability probing imported lark-oapi")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", tracking_import)
    monkeypatch.delitem(sys.modules, "lark_oapi", raising=False)
    namespace = {"__name__": "feishu_probe_test", "__file__": "test_feishu.py"}
    prefix = source.split("class _FakeRequestContent", 1)[0]
    exec(compile(prefix, "test_feishu.py", "exec"), namespace)

    assert attempted_imports == []
    assert namespace["_HAS_LARK_OAPI"] is (
        importlib.util.find_spec("lark_oapi") is not None
    )
    assert "lark_oapi" not in sys.modules
