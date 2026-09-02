from pathlib import Path


RUNNER = Path(__file__).resolve().parents[1] / "scripts" / "run_tests.sh"


def test_run_tests_preserves_selected_runtime_and_disables_lazy_installs():
    text = RUNNER.read_text()
    assert 'selected python:' in text
    assert 'linked SQLite:' in text
    assert 'HERMES_DISABLE_LAZY_INSTALLS=1' in text
    for name in ("TMPDIR", "HERMES_PYTHON", "LD_LIBRARY_PATH"):
        assert f'${{{name}:+{name}="${name}"}}' in text
