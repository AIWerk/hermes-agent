import importlib.util
import sqlite3
from pathlib import Path

import pytest


_CONFTST_PATH = Path(__file__).with_name("conftest.py")
_SPEC = importlib.util.spec_from_file_location("live_db_guard_conftest", _CONFTST_PATH)
assert _SPEC is not None and _SPEC.loader is not None
conftest = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(conftest)


def test_sqlite_target_path_resolves_plain_path(tmp_path):
    target = tmp_path / "nested" / "state.db"
    assert conftest._resolve_sqlite_target_path(target) == target.resolve()


def test_sqlite_target_path_resolves_file_uri_query_and_escaping(tmp_path):
    target = tmp_path / "folder with space" / "state.db"
    uri = target.as_uri().replace("%20", "%20") + "?mode=ro&immutable=1"
    assert conftest._resolve_sqlite_target_path(uri) == target.resolve()


def test_sqlite_target_path_ignores_non_filesystem_databases():
    assert conftest._resolve_sqlite_target_path(":memory:") is None
    assert conftest._resolve_sqlite_target_path("") is None


def test_live_hermes_path_classification_blocks_only_real_tree(tmp_path):
    live_home = tmp_path / ".hermes"
    assert conftest._is_path_within(live_home / "state.db", live_home)
    assert conftest._is_path_within(live_home, live_home)
    assert not conftest._is_path_within(tmp_path / ".hermes-copy" / "state.db", live_home)


def test_guarded_sqlite_connect_rejects_plain_and_file_uri_live_paths(tmp_path):
    live_home = tmp_path / ".hermes"
    calls = []

    def fake_connect(database, *args, **kwargs):
        calls.append((database, args, kwargs))
        return object()

    guarded = conftest._make_guarded_sqlite_connect(fake_connect, live_home)

    with pytest.raises(RuntimeError, match="live Hermes database access blocked"):
        guarded(live_home / "state.db")
    with pytest.raises(RuntimeError, match="live Hermes database access blocked"):
        guarded((live_home / "state.db").as_uri() + "?mode=ro", uri=True)
    assert calls == []


def test_guarded_sqlite_connect_forwards_temp_and_memory_targets(tmp_path):
    sentinel = object()
    calls = []

    def fake_connect(database, *args, **kwargs):
        calls.append((database, args, kwargs))
        return sentinel

    guarded = conftest._make_guarded_sqlite_connect(
        fake_connect, tmp_path / ".hermes"
    )
    target = tmp_path / "test-home" / "state.db"

    assert guarded(target, timeout=1) is sentinel
    assert guarded(":memory:") is sentinel
    assert calls == [(target, (), {"timeout": 1}), (":memory:", (), {})]


def test_default_db_path_tracks_per_test_hermes_home():
    import os

    import hermes_state

    assert hermes_state.DEFAULT_DB_PATH == Path(os.environ["HERMES_HOME"]) / "state.db"
