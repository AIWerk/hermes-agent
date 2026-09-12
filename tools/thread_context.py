"""Propagate agent-turn context into worker threads that dispatch Hermes tools.

A bare ``threading.Thread`` / ``ThreadPoolExecutor`` worker starts with an empty
``contextvars.Context`` and no thread-local approval/sudo callbacks, so tool dispatch inside it
silently loses the approval ContextVars (gateway sessions then auto-approve dangerous commands)
and the CLI callbacks (``prompt_dangerous_approval`` cannot reach the user, GHSA-qg5c-hvr5-hjgr).
Call :func:`propagate_context_to_thread` **on the parent thread** (it snapshots at call time) and
use the result as the worker target; callbacks are installed for the worker's lifetime and
always cleared on exit.
"""

from __future__ import annotations

import contextvars
import logging
from typing import Callable

logger = logging.getLogger(__name__)


def _callback_api():
    """Resolve callback getters/setters lazily to avoid import cycles."""
    from tools.terminal_tool import (
        _get_approval_callback,
        _get_sudo_password_callback,
        set_approval_callback,
        set_sudo_password_callback,
    )
    from hermes_cli.operator_verification import (
        _get_operator_verification_callback,
        set_operator_verification_callback,
    )
    from tools.skills_tool import (
        _get_secret_capture_callback,
        set_secret_capture_callback,
    )
    return (
        _get_approval_callback,
        _get_sudo_password_callback,
        set_approval_callback,
        set_sudo_password_callback,
        _get_operator_verification_callback,
        set_operator_verification_callback,
        _get_secret_capture_callback,
        set_secret_capture_callback,
    )


def propagate_context_to_thread(target: Callable) -> Callable:
    """Wrap *target* to run with the *current* thread's ContextVars and approval/sudo callbacks.

    Fail-closed: if callback installation raises they stay ``None`` — dangerous commands are then
    denied by ``prompt_dangerous_approval`` and the gateway approval queue blocks.
    """
    ctx = contextvars.copy_context()
    parent_approval_cb = parent_sudo_cb = parent_operator_cb = parent_secret_cb = None
    setters = None
    try:
        callback_api = _callback_api()
        if len(callback_api) == 6:
            # Backward-compatible for embedders/test doubles implementing the
            # pre-secret-capture callback tuple.
            callback_api = (*callback_api, lambda: None, lambda _value: None)
        (
            get_approval,
            get_sudo,
            set_approval,
            set_sudo,
            get_operator,
            set_operator,
            get_secret,
            set_secret,
        ) = callback_api
        parent_approval_cb = get_approval()
        parent_sudo_cb = get_sudo()
        parent_operator_cb = get_operator()
        parent_secret_cb = get_secret()
        setters = (set_approval, set_sudo, set_operator, set_secret)
    except Exception:
        logger.debug("Could not capture parent approval/sudo callbacks", exc_info=True)

    def _runner(*args, **kwargs):
        def _inner():
            def _clear_callbacks() -> bool:
                if setters is None:
                    return True
                cleared = True
                for setter in setters:
                    try:
                        setter(None)
                    except Exception:
                        cleared = False
                        logger.debug(
                            "Failed to clear propagated approval/sudo callback",
                            exc_info=True,
                        )
                return cleared

            if setters is not None:
                installed = True
                for setter, callback in zip(
                    setters,
                    (
                        parent_approval_cb,
                        parent_sudo_cb,
                        parent_operator_cb,
                        parent_secret_cb,
                    ),
                    strict=True,
                ):
                    try:
                        setter(callback)
                    except Exception:
                        installed = False
                        logger.debug(
                            "Failed to install propagated approval/sudo callback; "
                            "rolling back before tool dispatch",
                            exc_info=True,
                        )
                        break
                if not installed and not _clear_callbacks():
                    raise RuntimeError(
                        "Could not establish a clean approval callback context"
                    )
            try:
                return target(*args, **kwargs)
            finally:
                _clear_callbacks()

        return ctx.run(_inner)

    return _runner
