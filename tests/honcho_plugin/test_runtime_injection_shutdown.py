import threading
import time

from plugins.memory.honcho import HonchoMemoryProvider


def test_runtime_injection_flag_honors_host_override():
    class Config:
        host = "hermes.specialist"
        raw = {
            "injection": {"includeUserCard": True},
            "hosts": {
                "hermes.specialist": {
                    "injection": {"includeUserCard": False}
                }
            },
        }

    provider = HonchoMemoryProvider()
    provider._config = Config()
    assert provider._injection_flag("includeUserCard", True) is False


def test_honcho_session_end_drains_background_init_before_flush():
    provider = HonchoMemoryProvider()
    provider._config = type("Config", (), {"timeout": 1.0})()
    flush_called = threading.Event()

    class FlushManager:
        def flush_all(self):
            flush_called.set()

    def finish_init():
        time.sleep(0.05)
        provider._manager = FlushManager()
        provider._session_initialized = True

    provider._init_thread = threading.Thread(target=finish_init, daemon=True)
    provider._init_thread.start()
    provider.on_session_end([])

    assert not provider._init_thread.is_alive()
    assert flush_called.is_set()
