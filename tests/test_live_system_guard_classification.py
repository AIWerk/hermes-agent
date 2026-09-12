"""Behavioral signal-denial controls; no real signal reaches a target."""
import errno
import os
import signal
from types import SimpleNamespace

import psutil
import pytest

from tests import conftest


@pytest.fixture
def guard_probe(monkeypatch):
    """Install the real guard over a recording sink, before injecting lookup faults."""
    calls = []
    with monkeypatch.context() as patch:
        patch.setattr(os, "kill", lambda pid, sig, *a, **kw: calls.append((pid, sig)))
        request = SimpleNamespace(node=SimpleNamespace(get_closest_marker=lambda name: None))
        fixture = conftest._live_system_guard.__wrapped__(request, patch)
        next(fixture)
        try:
            yield calls, patch
        finally:
            fixture.close()


def _process(pid, parents=None, error=None, running=True):
    def ancestry():
        if error is not None:
            raise error
        return parents or []

    return SimpleNamespace(pid=pid, parents=ancestry, is_running=lambda: running,
                           terminate=lambda: os.kill(pid, signal.SIGTERM))


@pytest.mark.parametrize("error_kind", ["missing-ancestor", "access-denied", "unexpected"])
def test_unresolved_ancestry_does_not_abort_tree_cleanup(guard_probe, error_kind):
    from tools.process_registry import ProcessRegistry
    import tools.process_registry as registry_module

    calls, patch = guard_probe
    root_pid, unresolved_pid, sibling_pid = 910001, 910002, 910003
    error = {"missing-ancestor": psutil.NoSuchProcess(unresolved_pid),
             "access-denied": psutil.AccessDenied(unresolved_pid),
             "unexpected": RuntimeError("inspection failed")}[error_kind]
    own_parent = SimpleNamespace(pid=os.getpid())
    root = _process(root_pid, parents=[own_parent])
    unresolved = _process(unresolved_pid, error=error)
    sibling = _process(sibling_pid, parents=[own_parent])
    root.children = lambda recursive: [unresolved, sibling]
    targets = {p.pid: p for p in (root, unresolved, sibling)}
    patch.setattr(psutil, "Process", targets.__getitem__)
    patch.setattr(registry_module, "_IS_WINDOWS", False)
    patch.setattr(ProcessRegistry, "_daemon_term_grace_seconds", staticmethod(lambda: 0))

    ProcessRegistry._terminate_host_pid(root_pid)

    assert calls == [(sibling_pid, signal.SIGTERM), (root_pid, signal.SIGTERM)]


def test_verified_foreign_receives_no_signal(guard_probe):
    calls, patch = guard_probe
    patch.setattr(psutil, "Process", lambda pid: _process(pid, parents=[SimpleNamespace(pid=1)]))
    with pytest.raises(RuntimeError, match="outside the test process subtree"):
        os.kill(910004, signal.SIGTERM)
    assert calls == []


@pytest.mark.parametrize("error_kind", ["gone", "access-denied", "unexpected"])
def test_lookup_errors_fail_closed(guard_probe, error_kind):
    calls, patch = guard_probe
    pid = 910005
    error = {"gone": psutil.NoSuchProcess(pid), "access-denied": psutil.AccessDenied(pid),
             "unexpected": RuntimeError("inspection failed")}[error_kind]

    def lookup(pid):
        raise error

    patch.setattr(psutil, "Process", lookup)
    expected = ProcessLookupError if error_kind == "gone" else PermissionError
    with pytest.raises(expected) as raised:
        os.kill(pid, signal.SIGTERM)
    assert raised.value.errno == (errno.ESRCH if error_kind == "gone" else errno.EPERM)
    assert calls == []


@pytest.mark.parametrize("state", ["gone", "live", "identity-check-error"])
def test_ancestry_disappearance_distinguishes_target_from_ancestor(guard_probe, state):
    calls, patch = guard_probe
    pid = 910006
    process = _process(pid, error=psutil.NoSuchProcess(pid), running=state != "gone")
    if state == "identity-check-error":
        def denied():
            raise psutil.AccessDenied(pid)
        process.is_running = denied
    patch.setattr(psutil, "Process", lambda pid: process)
    expected = ProcessLookupError if state == "gone" else PermissionError
    with pytest.raises(expected):
        os.kill(pid, signal.SIGTERM)
    assert calls == []


def test_verified_own_receives_signal(guard_probe):
    calls, patch = guard_probe
    pid = 910007
    patch.setattr(psutil, "Process", lambda pid: _process(pid, parents=[SimpleNamespace(pid=os.getpid())]))
    os.kill(pid, signal.SIGTERM)
    assert calls == [(pid, signal.SIGTERM)]
