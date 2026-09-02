"""Restored regression coverage for thread-local skill secret capture."""

import threading

from tools.skills_tool import (
    _get_secret_capture_callback,
    set_secret_capture_callback,
)


def test_secret_capture_callback_set_and_get_in_same_thread():
    callback = lambda name, prompt, metadata=None: {"success": True}  # noqa: E731
    set_secret_capture_callback(callback)
    assert _get_secret_capture_callback() is callback
    set_secret_capture_callback(None)


def test_secret_capture_callback_isolated_across_threads():
    callback_a = lambda name, prompt, metadata=None: {"who": "a"}  # noqa: E731
    callback_b = lambda name, prompt, metadata=None: {"who": "b"}  # noqa: E731
    seen_in_a = []
    seen_in_b = []
    both_set = threading.Barrier(2)

    def thread_a():
        set_secret_capture_callback(callback_a)
        both_set.wait(timeout=2)
        seen_in_a.append(_get_secret_capture_callback())

    def thread_b():
        set_secret_capture_callback(callback_b)
        both_set.wait(timeout=2)
        seen_in_b.append(_get_secret_capture_callback())

    first = threading.Thread(target=thread_a)
    second = threading.Thread(target=thread_b)
    first.start()
    second.start()
    first.join()
    second.join()

    assert seen_in_a == [callback_a]
    assert seen_in_b == [callback_b]
