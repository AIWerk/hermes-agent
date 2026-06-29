#!/usr/bin/env python3
"""Propagate agent-turn context into worker threads that dispatch Hermes tools.

A bare ``threading.Thread`` / ``ThreadPoolExecutor`` worker starts with an
empty ``contextvars.Context`` and no thread-local approval/sudo callbacks.
Tool dispatch inside such a thread therefore silently loses:

  * the approval *session/platform* ContextVars (``tools.approval`` /
    ``gateway.session_context``) — so gateway sessions fall into
    ``check_dangerous_command``'s non-interactive auto-approve branch and
    dangerous commands run without prompting (#33057, #30882);
  * the thread-local CLI approval/sudo callbacks (``tools.terminal_tool``) —
    so ``prompt_dangerous_approval`` cannot reach the user
    (GHSA-qg5c-hvr5-hjgr, #15216);
  * the thread-local skills secret-capture callback (``tools.skills_tool``) —
    so an interactive skill-install running on a worker thread cannot prompt
    for secrets and silently falls through to ``setup_skipped``. The install
    path is sequential today, so this is propagated for consistency/defense
    in depth against a future parallelization of that tool.

This helper factors out that capture/install/clear lifecycle so the several
places that fan tool dispatch onto worker threads (``agent.tool_executor`` and
the ``execute_code`` RPC threads) share one audited implementation instead of
divergent copies.

Usage — call :func:`propagate_context_to_thread` **on the parent thread**
(it snapshots the parent's ContextVars and callbacks at call time) and use the
returned callable as the worker's target::

    t = threading.Thread(target=propagate_context_to_thread(loop_fn), args=(...))
    # or
    executor.submit(propagate_context_to_thread(worker_fn), *args)

Approval/sudo callbacks are installed for the worker's lifetime and **always
cleared on exit**, so a recycled thread never holds a stale reference to a
disposed CLI instance.
"""

from __future__ import annotations

import contextvars
import logging
from typing import Callable

logger = logging.getLogger(__name__)


def _callback_api():
    """Resolve the terminal_tool callback getters/setters.

    Imported lazily: ``tools.terminal_tool`` imports ``tools.approval`` at
    module load, so a top-level import here would risk an import cycle for
    callers that live in ``tools.approval``.
    """
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
    """Wrap *target* for execution on a worker thread with the *current*
    thread's ContextVars and approval/sudo callbacks propagated.

    Call this on the parent thread; pass the returned callable as the
    thread/executor target.  The returned callable forwards its positional
    and keyword arguments to *target* and returns its result.

    Fail-closed: if callback installation raises, the callbacks are left
    unset (``None``).  That is the safe outcome — ``prompt_dangerous_approval``
    denies dangerous commands when no callback is registered in an interactive
    context, and the gateway approval queue blocks when its notify callback is
    absent.
    """
    ctx = contextvars.copy_context()
    parent_approval_cb = parent_sudo_cb = parent_operator_cb = parent_secret_cb = None
    setters = None
    try:
        (
            get_approval,
            get_sudo,
            set_approval,
            set_sudo,
            get_operator,
            set_operator,
            get_secret,
            set_secret,
        ) = _callback_api()
        parent_approval_cb = get_approval()
        parent_sudo_cb = get_sudo()
        parent_operator_cb = get_operator()
        parent_secret_cb = get_secret()
        setters = (set_approval, set_sudo, set_operator, set_secret)
    except Exception:
        logger.debug("Could not capture parent approval/sudo callbacks", exc_info=True)

    def _runner(*args, **kwargs):
        def _inner():
            if setters is not None:
                set_approval, set_sudo, set_operator, set_secret = setters
                try:
                    if parent_approval_cb is not None:
                        set_approval(parent_approval_cb)
                    if parent_sudo_cb is not None:
                        set_sudo(parent_sudo_cb)
                    if parent_operator_cb is not None:
                        set_operator(parent_operator_cb)
                    if parent_secret_cb is not None:
                        set_secret(parent_secret_cb)
                except Exception:
                    logger.debug(
                        "Failed to install propagated approval/sudo callbacks; "
                        "dangerous-command approval will fail closed",
                        exc_info=True,
                    )
            try:
                return target(*args, **kwargs)
            finally:
                if setters is not None:
                    set_approval, set_sudo, set_operator, set_secret = setters
                    try:
                        set_approval(None)
                        set_sudo(None)
                        set_operator(None)
                        set_secret(None)
                    except Exception:
                        logger.debug(
                            "Failed to clear propagated approval/sudo callbacks",
                            exc_info=True,
                        )

        return ctx.run(_inner)

    return _runner
