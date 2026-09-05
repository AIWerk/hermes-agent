"""Regression coverage for the core DOCX XML dependency."""

from __future__ import annotations

import importlib.metadata
import tomllib
import zipfile
from pathlib import Path


def test_defusedxml_is_core_dependency_and_docx_extracts(tmp_path: Path) -> None:
    import defusedxml  # noqa: F401
    from hermes_cli.web_server import _extract_uploaded_text

    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert "defusedxml==0.7.1" in project["dependencies"]
    assert importlib.metadata.version("defusedxml") == "0.7.1"

    docx = tmp_path / "sample.docx"
    xml = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        b"<w:body><w:p><w:r><w:t>AIWerk sample</w:t></w:r></w:p></w:body></w:document>"
    )
    with zipfile.ZipFile(docx, "w") as archive:
        archive.writestr("word/document.xml", xml)

    text, note = _extract_uploaded_text(docx)

    assert note == "docx"
    assert text == "AIWerk sample"
    assert note != "docx-extraction-failed"
