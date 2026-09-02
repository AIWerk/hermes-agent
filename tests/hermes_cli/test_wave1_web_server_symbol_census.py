"""Wave 1 detector census: every historical web-server surface is present."""

import ast
import json
from pathlib import Path

import hermes_cli.web_server as web_server


_SCOPE = Path("/home/agbergsmann/aiwerk-recovery/wave1-scope-relative-current.json")


def test_wave1_web_server_historical_symbols_are_present():
    scope = json.loads(_SCOPE.read_text(encoding="utf-8"))
    assert scope["unaccounted_count"] == 0
    assert scope["unaccounted_by_path"] == {}
    names = scope["restored_by_path"]["hermes_cli/web_server.py"]
    assert len(names) == 88
    source = Path(web_server.__file__).read_text(encoding="utf-8")
    present = {
        node.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    missing = [name for name in names if name not in present]
    assert missing == []
