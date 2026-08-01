import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig


def _ensure_telegram_mock():
    if "telegram" in sys.modules and hasattr(sys.modules["telegram"], "__file__"):
        return
    telegram_mod = MagicMock()
    telegram_mod.ext.ContextTypes.DEFAULT_TYPE = type(None)
    telegram_mod.constants.ParseMode.MARKDOWN_V2 = "MarkdownV2"
    telegram_mod.constants.ChatType.GROUP = "group"
    telegram_mod.constants.ChatType.SUPERGROUP = "supergroup"
    telegram_mod.constants.ChatType.CHANNEL = "channel"
    telegram_mod.constants.ChatType.PRIVATE = "private"
    telegram_mod.error.NetworkError = type("NetworkError", (OSError,), {})
    telegram_mod.error.TimedOut = type("TimedOut", (OSError,), {})
    for name in ("telegram", "telegram.ext", "telegram.constants", "telegram.request"):
        sys.modules.setdefault(name, telegram_mod)
    sys.modules.setdefault("telegram.error", telegram_mod.error)


_ensure_telegram_mock()

from plugins.platforms.telegram import adapter as tg_adapter  # noqa: E402
from plugins.platforms.telegram.adapter import TelegramAdapter  # noqa: E402


@pytest.mark.asyncio
async def test_happy_path_connect_attempt_logs_at_info_not_warning(monkeypatch, caplog):
    """Normal Telegram startup progress should not look like a warning."""
    fake_app = MagicMock()
    fake_app.bot = MagicMock()
    fake_app.initialize = AsyncMock(return_value=None)
    fake_app.start = AsyncMock()
    fake_app.add_handler = MagicMock()

    chainable = MagicMock()
    chainable.token.return_value = chainable
    chainable.request.return_value = chainable
    chainable.get_updates_request.return_value = chainable
    chainable.build.return_value = fake_app

    builder_root = MagicMock()
    builder_root.builder.return_value = chainable
    monkeypatch.setattr(tg_adapter, "Application", builder_root)
    monkeypatch.setattr(tg_adapter, "HTTPXRequest", MagicMock)
    monkeypatch.setattr(tg_adapter, "discover_fallback_ips", AsyncMock(return_value=[]))
    monkeypatch.setattr(tg_adapter, "resolve_proxy_url", lambda *a, **k: None)
    monkeypatch.setattr(tg_adapter, "_await_with_thread_deadline", lambda awaitable, timeout, **_: awaitable)

    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    monkeypatch.setattr(adapter, "_acquire_platform_lock", lambda *a, **k: True)
    monkeypatch.setattr(adapter, "_fallback_ips", lambda: [])
    monkeypatch.setattr(adapter, "_delete_webhook_best_effort", AsyncMock())
    monkeypatch.setattr(adapter, "_start_polling_resilient", AsyncMock(return_value=True))
    monkeypatch.setattr(adapter, "_polling_heartbeat_loop", AsyncMock(return_value=None))
    monkeypatch.setattr(adapter, "_start_post_connect_housekeeping", MagicMock())

    caplog.set_level("INFO", logger=tg_adapter.logger.name)

    assert await adapter.connect() is True

    messages = [record.getMessage() for record in caplog.records]
    assert any("Connecting to Telegram (attempt 1/8)" in msg for msg in messages)
    assert not any(
        record.levelname == "WARNING" and "Connecting to Telegram" in record.getMessage()
        for record in caplog.records
    )
    assert not any(
        record.levelname == "WARNING" and "Discovering Telegram API fallback IPs" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_connect_retries_when_initialize_wall_deadline_expires(monkeypatch):
    """A wedged initialize() attempt must not trap startup on attempt 1/8."""
    fake_app = MagicMock()
    fake_app.bot = MagicMock()
    fake_app.initialize = AsyncMock(return_value=None)
    fake_app.start = AsyncMock()
    fake_app.add_handler = MagicMock()

    chainable = MagicMock()
    chainable.token.return_value = chainable
    chainable.request.return_value = chainable
    chainable.get_updates_request.return_value = chainable
    chainable.build.return_value = fake_app

    builder_root = MagicMock()
    builder_root.builder.return_value = chainable
    monkeypatch.setattr(tg_adapter, "Application", builder_root)
    monkeypatch.setattr(tg_adapter, "HTTPXRequest", MagicMock)
    monkeypatch.setattr(tg_adapter, "discover_fallback_ips", AsyncMock(return_value=[]))
    monkeypatch.setattr(tg_adapter, "resolve_proxy_url", lambda *a, **k: None)
    monkeypatch.setattr(tg_adapter.asyncio, "sleep", AsyncMock())

    deadline_calls = 0

    async def _fake_deadline(awaitable, timeout, *, on_abandon=None):
        nonlocal deadline_calls
        deadline_calls += 1
        if deadline_calls == 1:
            awaitable.close()
            raise tg_adapter.asyncio.TimeoutError()
        return await awaitable

    monkeypatch.setattr(tg_adapter, "_await_with_thread_deadline", _fake_deadline)

    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    monkeypatch.setattr(adapter, "_acquire_platform_lock", lambda *a, **k: True)
    monkeypatch.setattr(adapter, "_fallback_ips", lambda: [])
    monkeypatch.setattr(adapter, "_delete_webhook_best_effort", AsyncMock())
    monkeypatch.setattr(adapter, "_start_polling_resilient", AsyncMock(return_value=True))
    monkeypatch.setattr(adapter, "_polling_heartbeat_loop", AsyncMock(return_value=None))
    monkeypatch.setattr(adapter, "_start_post_connect_housekeeping", MagicMock())

    assert await adapter.connect() is True

    assert fake_app.initialize.call_count == 2
    assert fake_app.initialize.await_count == 1
    assert deadline_calls == 2
    tg_adapter.asyncio.sleep.assert_awaited_once_with(1)
    fake_app.start.assert_awaited_once()


@pytest.mark.asyncio
async def test_await_with_thread_deadline_returns_value_on_happy_path():
    """The real helper returns the awaited result and raises no timeout."""
    async def _ok():
        return 42

    result = await tg_adapter._await_with_thread_deadline(_ok(), timeout=5.0)
    assert result == 42


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

    dumps = []
    monkeypatch.setattr(
        tg_adapter,
        "_dump_loop_blocked_diagnostics",
        lambda timeout, grace: dumps.append((timeout, grace)),
    )
    monkeypatch.setattr(tg_adapter, "_LOOP_BLOCKED_DUMP_GRACE", 0.15)

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

    assert dumps == [(0.05, 0.15)]
    hung.cancel()
