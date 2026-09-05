"""Regression coverage for native clipboard and OSC 52 fallback."""

from __future__ import annotations


def test_native_clipboard_uses_wayland_tool(monkeypatch) -> None:
    from hermes_cli import clipboard

    calls: list[tuple[list[str], bytes]] = []

    class Result:
        returncode = 0

    def run(argv, **kwargs):
        calls.append((argv, kwargs["input"]))
        return Result()

    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setattr(clipboard.sys, "platform", "linux")
    monkeypatch.setattr(clipboard, "_is_wsl", lambda: False)
    monkeypatch.setattr(clipboard.subprocess, "run", run)

    assert clipboard.write_clipboard_text("hello") is True
    assert calls == [(["wl-copy", "--type", "text/plain"], b"hello")]


def test_all_ssh_markers_select_remote_clipboard_boundary() -> None:
    from hermes_cli.clipboard import is_remote_shell_session

    for marker in ("SSH_CONNECTION", "SSH_TTY", "SSH_CLIENT"):
        assert is_remote_shell_session({marker: "present"}) is True


def test_osc52_fallback_never_retries_native_clipboard(monkeypatch) -> None:
    import cli

    def reject_native_attempt(*_args, **_kwargs):
        raise AssertionError("OSC 52 fallback must not retry native clipboard tools")

    monkeypatch.setattr(
        cli.HermesCLI, "_write_native_clipboard", reject_native_attempt, raising=False
    )
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    instance = object.__new__(cli.HermesCLI)
    writes: list[str] = []

    class Output:
        def write_raw(self, text: str) -> None:
            writes.append(text)

        def flush(self) -> None:
            return None

    class App:
        output = Output()

    instance._app = App()  # type: ignore[assignment]
    instance._write_osc52_clipboard("secret")

    assert len(writes) == 1
    assert writes[0].startswith("\x1b]52;c;")
