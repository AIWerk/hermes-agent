from contextlib import nullcontext

from hermes_state import SessionDB
from hermes_state_common import SCHEMA_SQL


def _selected_names(sql: str) -> set[str]:
    return {part.strip().removeprefix("s.") for part in sql.split(",") if part.strip()}


def test_every_declared_session_column_has_an_explicit_visibility_classification():
    declared = set(SessionDB._parse_schema_columns(SCHEMA_SQL)["sessions"])

    assert set(SessionDB._SESSION_COLUMN_VISIBILITY) == declared
    assert set(SessionDB._SESSION_COLUMN_VISIBILITY.values()) <= {
        "public",
        "internal",
        "secret",
    }


def test_public_compact_projection_is_allowlisted_and_omits_internal_metadata():
    public = _selected_names(SessionDB._compact_session_cols("public"))

    assert "id" in public
    assert "git_branch" in public
    assert "system_prompt_hash" not in public
    assert "git_metadata_generation" not in public
    assert "system_prompt" not in public


def test_internal_compact_projection_is_distinct_but_still_omits_secret_blob():
    internal = _selected_names(SessionDB._compact_session_cols("internal"))

    assert "system_prompt_hash" in internal
    assert "git_metadata_generation" in internal
    assert "system_prompt" not in internal


def test_batch_rich_lookup_propagates_internal_compact_visibility():
    seen = []

    class Cursor:
        def fetchall(self):
            return []

    class Conn:
        def execute(self, _query, _params):
            return Cursor()

    class Fake:
        _lock = nullcontext()
        _conn = Conn()

        def flush_token_counts(self):
            return None

        def _compact_session_cols(self, visibility="public"):
            seen.append(visibility)
            return "s.id"

    SessionDB._get_session_rich_rows_batch(
        Fake(), ["tip"], compact_rows=True, compact_visibility="internal"
    )

    assert seen == ["internal"]
