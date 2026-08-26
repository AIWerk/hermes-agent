import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram import adapter as tg_adapter  # noqa: E402
from plugins.platforms.telegram.adapter import TelegramAdapter  # noqa: E402


def _mock_connect_dependencies(monkeypatch):
    fake_app = MagicMock()
    fake_app.bot = MagicMock()
    fake_app.initialize = MagicMock()
    fake_app.start = AsyncMock()

    builder = MagicMock()
    builder.token.return_value = builder
    builder.request.return_value = builder
    builder.get_updates_request.return_value = builder
    builder.build.return_value = fake_app

    application = MagicMock()
    application.builder.return_value = builder
    monkeypatch.setattr(tg_adapter, "Application", application)
    monkeypatch.setattr(tg_adapter, "HTTPXRequest", MagicMock)
    monkeypatch.setattr(tg_adapter, "discover_fallback_ips", AsyncMock(return_value=[]))
    monkeypatch.setattr(tg_adapter, "resolve_proxy_url", lambda *args, **kwargs: None)
    monkeypatch.setattr(tg_adapter, "_shutdown_abandoned_app", AsyncMock())

    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    monkeypatch.setattr(adapter, "_acquire_platform_lock", lambda *args, **kwargs: True)
    monkeypatch.setattr(adapter, "_fallback_ips", lambda: [])
    monkeypatch.setattr(adapter, "_instrument_polling_request", lambda request: request)
    monkeypatch.setattr(adapter, "_register_handlers", lambda app: None)
    monkeypatch.setattr(adapter, "_delete_webhook_best_effort", AsyncMock())
    monkeypatch.setattr(adapter, "_start_polling_resilient", AsyncMock(return_value=True))
    monkeypatch.setattr(adapter, "_polling_heartbeat_loop", AsyncMock(return_value=None))
    monkeypatch.setattr(adapter, "_start_post_connect_housekeeping", MagicMock())
    return adapter, fake_app


@pytest.mark.asyncio
async def test_normal_startup_progress_logs_at_info_not_warning(monkeypatch, caplog):
    """Fallback discovery and an ordinary connect attempt are INFO progress."""
    adapter, fake_app = _mock_connect_dependencies(monkeypatch)

    async def _initialize():
        return None

    fake_app.initialize.side_effect = _initialize
    caplog.set_level(logging.INFO, logger=tg_adapter.logger.name)

    assert await adapter.connect() is True

    progress_fragments = (
        "Discovering Telegram API fallback IPs",
        "Connecting to Telegram (attempt 1/8)",
    )
    for fragment in progress_fragments:
        matching = [
            record for record in caplog.records if fragment in record.getMessage()
        ]
        assert matching
        assert {record.levelno for record in matching} == {logging.INFO}


@pytest.mark.asyncio
async def test_deadline_timeout_and_retry_exhaustion_retain_warning_and_error(
    monkeypatch, caplog
):
    """Real deadline trouble stays noisy through retry exhaustion."""
    adapter, fake_app = _mock_connect_dependencies(monkeypatch)
    monkeypatch.setenv("HERMES_TELEGRAM_DISABLE_FALLBACK_IPS", "1")
    monkeypatch.setattr(tg_adapter.asyncio, "sleep", AsyncMock())

    async def _timeout(awaitable, timeout, **kwargs):
        awaitable.close()
        raise tg_adapter.asyncio.TimeoutError()

    fake_app.initialize.side_effect = lambda: _never_awaited()
    monkeypatch.setattr(tg_adapter, "_await_with_thread_deadline", _timeout)
    caplog.set_level(logging.INFO, logger=tg_adapter.logger.name)

    assert await adapter.connect() is False

    timeout_records = [
        record
        for record in caplog.records
        if "timed out after" in record.getMessage()
        and "retrying" in record.getMessage()
    ]
    assert len(timeout_records) == 7
    assert {record.levelno for record in timeout_records} == {logging.WARNING}
    exhausted = [
        record
        for record in caplog.records
        if "Failed to connect to Telegram" in record.getMessage()
    ]
    assert exhausted
    assert {record.levelno for record in exhausted} == {logging.ERROR}


@pytest.mark.asyncio
async def test_failed_attempt_retains_warning(monkeypatch, caplog):
    """A transient initialize failure remains a warning before recovery."""
    adapter, fake_app = _mock_connect_dependencies(monkeypatch)
    monkeypatch.setenv("HERMES_TELEGRAM_DISABLE_FALLBACK_IPS", "1")
    monkeypatch.setattr(tg_adapter.asyncio, "sleep", AsyncMock())
    attempts = 0

    async def _deadline(awaitable, timeout, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            awaitable.close()
            raise OSError("temporary transport failure")
        return await awaitable

    fake_app.initialize.side_effect = lambda: _never_awaited()
    monkeypatch.setattr(tg_adapter, "_await_with_thread_deadline", _deadline)
    caplog.set_level(logging.INFO, logger=tg_adapter.logger.name)

    assert await adapter.connect() is True

    failures = [
        record
        for record in caplog.records
        if "Connect attempt 1/8 failed" in record.getMessage()
    ]
    assert failures
    assert {record.levelno for record in failures} == {logging.WARNING}


async def _never_awaited():
    return None


@pytest.mark.asyncio
async def test_await_with_thread_deadline_abandons_and_runs_cleanup_on_timeout():
    """A wedged awaitable must raise TimeoutError promptly AND trigger the
    best-effort on_abandon cleanup (the httpx-pool-leak guard).

    This exercises the REAL _await_with_thread_deadline (not a monkeypatched
    stub), covering the abandonment + cleanup mechanism directly.
    """
    import asyncio as _asyncio
    import time as _time

    cleanup_ran = _asyncio.Event()

    async def _wedged():
        # Swallows cancellation for a bounded window — long enough that the
        # helper must return control BEFORE this finishes (proving it doesn't
        # await cancellation, the #58236 shielded-scope behavior), but bounded
        # so the abandoned task can't outlive the test and wedge teardown.
        for _ in range(20):
            try:
                await _asyncio.sleep(0.05)
            except _asyncio.CancelledError:
                # Keep going despite cancellation, like the shielded scope.
                pass

    async def _cleanup():
        cleanup_ran.set()

    started = _time.monotonic()
    with pytest.raises(_asyncio.TimeoutError):
        await tg_adapter._await_with_thread_deadline(
            _wedged(), timeout=0.2, on_abandon=_cleanup
        )
    elapsed = _time.monotonic() - started

    # Returned control promptly — well before the wedged coroutine's ~1s span.
    assert elapsed < 0.8
    # The detached cleanup was scheduled; give the loop a tick to run it.
    await _asyncio.wait_for(cleanup_ran.wait(), timeout=2.0)
    assert cleanup_ran.is_set()


@pytest.mark.asyncio
async def test_await_with_thread_deadline_cleanup_error_is_swallowed():
    """A cleanup that raises must not surface as an unhandled task error."""
    import asyncio as _asyncio

    async def _wedged():
        for _ in range(20):
            try:
                await _asyncio.sleep(0.05)
            except _asyncio.CancelledError:
                pass

    def _boom():
        raise RuntimeError("cleanup blew up")

    # Must still raise TimeoutError (not the cleanup error) and not crash.
    with pytest.raises(_asyncio.TimeoutError):
        await tg_adapter._await_with_thread_deadline(
            _wedged(), timeout=0.2, on_abandon=_boom
        )
    # Let the detached cleanup task run and be observed (no unraised error).
    await _asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_blocked_loop_after_expiry_dumps_diagnostics(monkeypatch):
    """#63309: when the loop thread is stuck in a synchronous call, the expiry
    callback never runs and every asyncio timeout goes silent. The off-loop
    watchdog must detect that state and emit diagnostics from its own thread."""
    import asyncio as _asyncio
    import time as _time

    from agent import deadline as _deadline

    dumps = []
    monkeypatch.setattr(
        _deadline,
        "_dump_blocked_loop_diagnostics",
        lambda label, timeout_s: dumps.append((label, timeout_s)),
    )
    monkeypatch.setattr(_deadline, "_LOOP_BLOCKED_DUMP_GRACE_S", 0.15)

    hung = _asyncio.get_running_loop().create_future()  # never completes
    task = _asyncio.ensure_future(
        tg_adapter._await_with_thread_deadline(hung, timeout=0.05)
    )
    # Let the helper start its deadline + watchdog timers…
    await _asyncio.sleep(0)
    # …then block the event loop straight through deadline (0.05s) AND the
    # watchdog grace (0.15s): call_soon_threadsafe stays queued, exactly like
    # a sync call pinning the loop during Application.initialize().
    # Margin matters: the watchdog thread only dumps if the loop is STILL
    # blocked when it wakes, and thread wakeup lags under parallel-suite load.
    # 0.2s (= deadline+grace exactly) flaked in a 40-worker full-suite run.
    _time.sleep(1.0)
    with pytest.raises(_asyncio.TimeoutError):
        await task

    assert dumps == [("telegram-init", 0.05)]
    hung.cancel()


