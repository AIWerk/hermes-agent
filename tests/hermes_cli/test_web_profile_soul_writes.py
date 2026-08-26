"""``PUT /api/profiles/{name}/soul`` must not destroy an existing SOUL.md.

The dashboard persona editor replaces the whole document on every Save. A bare
``write_text()`` truncates SOUL.md before the new body lands, and the paired
``GET`` reports an unreadable file as ``{"content": "", "exists": False}`` — so
an interrupted save presents as "your persona was never set" and the editor's
next Save persists that empty document over the original.

Lives in its own module rather than ``test_web_server.py`` to keep the harness
small and focused on this one endpoint pair.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from agent.prompt_builder import DEFAULT_AGENT_IDENTITY
from hermes_cli.default_soul import DEFAULT_SOUL_MD


SOUL = "# Persona\n\nYou are a careful, terse assistant.\n"
REPO_ROOT = Path(__file__).resolve().parents[2]


def _normalize_prose(text: str) -> str:
    return " ".join(text.split())


def _extract_single_body(text: str, opening: str, closing: str) -> str:
    assert text.count(opening) == 1
    remainder = text.split(opening, 1)[1]
    assert closing in remainder
    return remainder.split(closing, 1)[0].replace("\r\n", "\n").strip()


def test_default_soul_concise_guidance_is_synchronized():
    canonical = (
        "You are Hermes Agent, an intelligent AI assistant created by Nous Research. "
        "You are helpful, knowledgeable, direct, and concise. You assist users with a "
        "wide range of tasks including answering questions, writing and editing code, "
        "analyzing information, creative work, and executing actions via your tools. "
        "You communicate clearly, admit uncertainty when appropriate, and prioritize "
        "being genuinely useful over being verbose unless otherwise directed below. "
        "Optimize for useful signal over verbosity: keep answers tight, summarize tool "
        "outputs instead of pasting long raw logs, and avoid unnecessary explanation. "
        "Be targeted and token-efficient in your exploration and investigations. When "
        "the conversation context is close to the limit, warn the user briefly."
    )
    required_phrases = (
        "You communicate clearly",
        "admit uncertainty when appropriate",
        "prioritize being genuinely useful over being verbose unless otherwise directed below",
        "direct, and concise",
        "Optimize for useful signal over verbosity",
        "summarize tool outputs instead of pasting long raw logs",
        "avoid unnecessary explanation",
        "targeted and token-efficient",
        "conversation context is close to the limit",
    )
    for identity in (DEFAULT_SOUL_MD, DEFAULT_AGENT_IDENTITY):
        assert all(fragment in identity for fragment in required_phrases)
    assert DEFAULT_SOUL_MD == DEFAULT_AGENT_IDENTITY == canonical

    docker_soul = (REPO_ROOT / "docker" / "SOUL.md").read_text(encoding="utf-8")
    assert docker_soul.rstrip("\r\n") == canonical

    install_sh = (REPO_ROOT / "scripts/install.sh").read_text(encoding="utf-8")
    assert (
        _extract_single_body(
            install_sh,
            'cat > "$HERMES_HOME/SOUL.md" << \'SOUL_EOF\'\n',
            "\nSOUL_EOF",
        )
        == canonical
    )

    install_ps1 = (REPO_ROOT / "scripts/install.ps1").read_text(encoding="utf-8")
    assert _extract_single_body(install_ps1, '$soulContent = @"\n', '\n"@') == canonical

    english_docs = (REPO_ROOT / "website/docs/developer-guide/prompt-assembly.md").read_text(
        encoding="utf-8"
    )
    assert _normalize_prose(
        "You are Hermes Agent"
        + _extract_single_body(english_docs, "```\nYou are Hermes Agent", "\n```")
    ) == _normalize_prose(canonical)

    chinese_docs = (
        REPO_ROOT
        / "website/i18n/zh-Hans/docusaurus-plugin-content-docs/current/developer-guide/prompt-assembly.md"
    ).read_text(encoding="utf-8")
    chinese_fallback = "You are Hermes Agent" + _extract_single_body(
        chinese_docs, "```\nYou are Hermes Agent", "\n```"
    )
    assert _normalize_prose(chinese_fallback) == _normalize_prose(canonical)
    chinese_explanation = chinese_docs.split(chinese_fallback + "\n```", 1)[1].split(
        "\n## ", 1
    )[0]
    for phrase in (
        "清晰沟通",
        "在适当情况下坦诚不确定性",
        "真正有用",
        "后续明确指示可覆盖这些默认偏好",
        "简洁",
        "信息密度高",
        "概括冗长的工具输出",
        "不必要的解释",
        "token",
        "上下文接近上限",
    ):
        assert phrase in chinese_explanation


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_DASHBOARD_SESSION_TOKEN", "soul-test-token")
    from hermes_cli import web_server

    with TestClient(web_server.app, raise_server_exceptions=False) as c:
        c.headers["Authorization"] = "Bearer soul-test-token"
        yield c


@pytest.fixture()
def profile_dir(tmp_path, monkeypatch) -> Path:
    """Create a real profile directory under the test HERMES_HOME."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli import profiles as profiles_mod

    d = profiles_mod.get_profile_dir("demo")
    d.mkdir(parents=True, exist_ok=True)
    return d


class TestSoulWriteDurability:
    def test_put_replaces_soul(self, client, profile_dir: Path):
        """Happy path: the editor's Save still works."""
        (profile_dir / "SOUL.md").write_text(SOUL, encoding="utf-8")

        r = client.put("/api/profiles/demo/soul", json={"content": "# New\n"})

        assert r.status_code == 200, r.text
        assert (profile_dir / "SOUL.md").read_text(encoding="utf-8") == "# New\n"

    def test_put_creates_soul_when_absent(self, client, profile_dir: Path):
        """A first save has no prior file to preserve permissions from."""
        assert not (profile_dir / "SOUL.md").exists()

        r = client.put("/api/profiles/demo/soul", json={"content": SOUL})

        assert r.status_code == 200, r.text
        assert (profile_dir / "SOUL.md").read_text(encoding="utf-8") == SOUL

    def test_existing_soul_survives_an_interrupted_save(
        self, client, profile_dir: Path
    ):
        soul = profile_dir / "SOUL.md"
        soul.write_text(SOUL, encoding="utf-8")
        original = soul.read_bytes()

        def boom(fd):
            raise OSError("simulated crash mid-write")

        # Scoped context so restoring os.fsync doesn't also undo the
        # HERMES_HOME patch the client/profile_dir fixtures installed.
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(os, "fsync", boom)
            r = client.put(
                "/api/profiles/demo/soul", json={"content": "# clobbered\n"}
            )

        assert r.status_code == 500
        # The persona the user already had must survive verbatim...
        assert soul.read_bytes() == original
        # ...and the paired GET must not report it as never-set, which is what
        # would make the next Save persist an empty document.
        g = client.get("/api/profiles/demo/soul")
        assert g.status_code == 200, g.text
        assert g.json()["exists"] is True
        assert g.json()["content"] == SOUL
        # No temp file left behind in the profile directory.
        assert list(profile_dir.glob("*.tmp")) == []

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
    def test_existing_file_mode_is_preserved(self, client, profile_dir: Path):
        """Profile SOUL.md is created 0644 and never run through
        ``_secure_file``; saving from the dashboard must not change that."""
        soul = profile_dir / "SOUL.md"
        soul.write_text(SOUL, encoding="utf-8")
        os.chmod(soul, 0o644)

        r = client.put("/api/profiles/demo/soul", json={"content": "# New\n"})

        assert r.status_code == 200, r.text
        mode = stat.S_IMODE(soul.stat().st_mode)
        assert mode == 0o644, f"mode changed to {oct(mode)}"

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
    def test_created_file_mode_is_not_tightened(self, client, profile_dir: Path):
        """The first-ever Save must not leave SOUL.md owner-only.

        There is no prior file to copy permissions from, and
        ``atomic_write_text`` swaps in a ``tempfile.mkstemp`` file (0600).
        Profile creation seeds SOUL.md with a plain ``write_text()`` and
        chmods only ``.env`` to 0600, so routing this endpoint through the
        atomic writer must not tighten the persona document as a side effect.
        """
        soul = profile_dir / "SOUL.md"
        assert not soul.exists()

        r = client.put("/api/profiles/demo/soul", json={"content": SOUL})

        assert r.status_code == 200, r.text
        mode = stat.S_IMODE(soul.stat().st_mode)
        assert mode == 0o644, f"first save created SOUL.md as {oct(mode)}"
