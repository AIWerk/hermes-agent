"""Propagate agent-turn context into worker threads that dispatch Hermes tools."""

from __future__ import annotations

import contextvars
import logging
from typing import Callable

logger = logging.getLogger(__name__)


def _callback_api():
    """Resolve every thread-local prompt callback as canonical getter/setter pairs."""
    from tools import terminal_tool as tt
    from hermes_cli.operator_verification import (
        _get_operator_verification_callback,
        set_operator_verification_callback,
    )
    from tools.skills_tool import _get_secret_capture_callback, set_secret_capture_callback
    from agent.vault_backends import unlock as vault_unlock

    return (
        (tt._get_approval_callback, tt.set_approval_callback),
        (tt._get_sudo_password_callback, tt.set_sudo_password_callback),
        (_get_operator_verification_callback, set_operator_verification_callback),
        (_get_secret_capture_callback, set_secret_capture_callback),
        (vault_unlock.get_unlock_prompt_callback, vault_unlock.set_unlock_prompt_callback),
        (vault_unlock.get_save_login_prompt_callback, vault_unlock.set_save_login_prompt_callback),
        (vault_unlock.get_code_prompt_callback, vault_unlock.set_code_prompt_callback),
    )


def propagate_context_to_thread(target: Callable) -> Callable:
    """Snapshot ContextVars and prompt callbacks on the parent and install them for one worker call."""
    ctx = contextvars.copy_context()
    installs: tuple[tuple[Callable, object], ...] = ()
    try:
        installs = tuple((setter, getter()) for getter, setter in _callback_api())
    except Exception:
        logger.debug("Could not capture parent prompt callbacks", exc_info=True)

    def _runner(*args, **kwargs):
        def _inner():
            def _clear_callbacks() -> bool:
                cleared = True
                for setter, _callback in installs:
                    try:
                        setter(None)
                    except Exception:
                        cleared = False
                        logger.debug("Failed to clear propagated prompt callback", exc_info=True)
                return cleared

            try:
                for setter, callback in installs:
                    setter(callback)
            except Exception as exc:
                _clear_callbacks()
                raise RuntimeError("Could not establish a clean prompt callback context") from exc
            try:
                return target(*args, **kwargs)
            finally:
                _clear_callbacks()

        return ctx.run(_inner)

    return _runner
