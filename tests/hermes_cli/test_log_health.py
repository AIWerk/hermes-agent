from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from hermes_cli.log_health import classify, parse_ts, redact, render_report, signature


def test_redacts_secrets_and_query_tokens() -> None:
    line = "Authorization: Bearer sk-testsecret1234567890 url=https://x.test?a=1&token=abc123"

    redacted = redact(line)

    assert "sk-testsecret" not in redacted
    assert "abc123" not in redacted
    assert "[REDACTED]" in redacted or "[REDACTED_TOKEN]" in redacted


def test_classifies_critical_and_warning_patterns() -> None:
    assert classify("Traceback (most recent call last):") == "red"
    assert classify("HTTP 401 unauthorized from provider") == "red"
    assert classify("upstream_timeout during compression") == "yellow"
    assert classify("everything is fine") is None


def test_parse_ts_handles_common_log_timestamp() -> None:
    dt = parse_ts("2026-07-05 17:48:12,123 something happened")

    assert dt is not None
    assert dt.year == 2026
    assert dt.month == 7
    assert dt.day == 5


def test_signature_deduplicates_timestamps_and_numbers() -> None:
    a = signature("2026-07-05 17:48:12 status=401 latency=123")
    b = signature("2026-07-05 17:49:13 status=402 latency=999")

    assert a == b


def test_render_report_ignores_old_timestamped_traceback_continuations(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "agent.log").write_text(
        "\n".join(
            [
                "2026-07-05 10:00:00 ERROR old failure",
                "Traceback (most recent call last):",
                "  File \"old.py\", line 1, in <module>",
                "Permission denied in old traceback continuation",
                "2026-07-05 17:50:00 WARNING recent warning only",
            ]
        ),
        encoding="utf-8",
    )

    report = render_report(
        agent="Test Agent",
        log_dir=logs,
        hours=1,
        max_examples=10,
        now=datetime(2026, 7, 5, 18, 0, tzinfo=ZoneInfo("Europe/Zurich")),
    )

    assert "kritikus=0" in report
    assert "figyelmeztetés=1" in report
    assert "recent warning only" in report
    assert "old traceback" not in report
    assert "Permission denied" not in report


def test_render_report_keeps_untimestamped_standalone_errors(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "agent.log").write_text("fatal startup error before logger configured\n", encoding="utf-8")

    report = render_report(
        agent="Test Agent",
        log_dir=logs,
        hours=1,
        max_examples=10,
        now=datetime(2026, 7, 5, 18, 0, tzinfo=ZoneInfo("Europe/Zurich")),
    )

    assert "kritikus=1" in report
    assert "fatal startup error" in report


def test_render_report_surfaces_log_read_errors(tmp_path: Path, monkeypatch) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "agent.log").write_text("2026-07-05 17:50:00 all good\n", encoding="utf-8")
    original_read_text = Path.read_text

    def raising_read_text(self: Path, *args, **kwargs):
        if self.name == "agent.log":
            raise OSError("boom")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", raising_read_text)

    report = render_report(
        agent="Test Agent",
        log_dir=logs,
        hours=1,
        max_examples=10,
        now=datetime(2026, 7, 5, 18, 0, tzinfo=ZoneInfo("Europe/Zurich")),
    )

    assert "kritikus=1" in report
    assert "LOG_READ_ERROR agent.log" in report


def test_render_report_is_read_only_redacted_and_deduplicated(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "agent.log").write_text(
        "\n".join(
            [
                "2026-07-05 17:48:12 Traceback (most recent call last): token=secret-value",
                "2026-07-05 17:49:12 Traceback (most recent call last): token=another-secret",
                "2026-07-05 17:50:12 upstream_timeout from provider",
            ]
        ),
        encoding="utf-8",
    )

    report = render_report(
        agent="Test Agent",
        log_dir=logs,
        hours=12.5,
        max_examples=10,
        now=datetime(2026, 7, 5, 18, 0, tzinfo=ZoneInfo("Europe/Zurich")),
    )

    assert "Test Agent" in report
    assert "kritikus=1" in report
    assert "figyelmeztetés=1" in report
    assert "secret-value" not in report
    assert "another-secret" not in report
    assert "Read-only report" in report


def test_render_report_can_be_quiet_on_ok(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "agent.log").write_text("2026-07-05 17:48:12 all good\n", encoding="utf-8")

    report = render_report(
        agent="Test Agent",
        log_dir=logs,
        hours=12.5,
        max_examples=10,
        quiet_ok=True,
        now=datetime(2026, 7, 5, 18, 0, tzinfo=ZoneInfo("Europe/Zurich")),
    )

    assert report == ""
