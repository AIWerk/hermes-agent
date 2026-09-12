"""``hermes doctor`` — diagnose (and with --fix, repair) a Hermes install.

``run_doctor`` walks ``DOCTOR_CHECKS`` in order; each check prints its own rows and returns a ``Finding``.
Check bodies live in the ``doctor_*`` siblings.
"""

import os
import sys
from pathlib import Path

from hermes_cli.config import (
    detect_install_method,
    get_env_path,
    get_hermes_home,
    get_project_root,
    is_nix_install_method,
    recommended_update_command_for_method,
)
from hermes_cli.env_loader import load_hermes_dotenv
from hermes_constants import display_hermes_home

PROJECT_ROOT = get_project_root()
HERMES_HOME = get_hermes_home()
_DHH = display_hermes_home()  # user-facing display path (e.g. ~/.hermes or ~/.hermes/profiles/coder)

# Load environment variables from ~/.hermes/.env so API key checks work
_env_path = get_env_path()
load_hermes_dotenv(hermes_home=_env_path.parent, project_env=PROJECT_ROOT / ".env")

from hermes_cli.colors import Colors, color
from hermes_cli.doctor_report import Finding, _section, check_bool, check_info, doctor_check, warn_on_error
from hermes_cli.doctor_connectivity import _has_healthy_oauth_fallback_for_apikey_provider, build_probes, run_probes
from hermes_cli.doctor_tools import _safe_which

from hermes_cli.doctor_config import (
    _check_config_drift,
    _check_config_file,
    _check_env_file,
    _check_mcp_security,
    _check_xai_retirement,
    _check_plugin_compat,
)
from hermes_cli.doctor_platform import (
    _check_certificates,
    _check_command_installation,
    _check_gateway_supervision,
    _check_python_environment,
    _check_required_packages,
    _check_security_advisories,
)


from hermes_constants import is_termux as _is_termux


def _python_install_cmd() -> str:
    return "python -m pip install" if _is_termux() else "uv pip install"


def _system_package_install_cmd(pkg: str) -> str:
    if _is_termux():
        return f"pkg install {pkg}"
    if sys.platform == "darwin":
        return f"brew install {pkg}"
    return f"sudo apt install {pkg}"


def _sqlite_upgrade_hint(install_method: str | None = None) -> str:
    """Return an actionable SQLite upgrade hint for this install layout."""
    method = install_method or detect_install_method(PROJECT_ROOT)
    if method == "docker":
        command = recommended_update_command_for_method(method)
        action = f"run `{command}`, then recreate all Hermes containers"
    elif is_nix_install_method(method):
        # The Nix helper is prose guidance, not a literal shell command.
        action = recommended_update_command_for_method(method)
    elif method == "apt":
        action = f"run `{recommended_update_command_for_method(method)}`"
    else:
        action = "run `hermes update`"
    return (
        f"({action}; fixed versions: 3.51.3+ / 3.50.7 / 3.44.6 — "
        "see https://sqlite.org/wal.html#walresetbug)"
    )


def _hermes_database_paths(hermes_home: Path) -> list[tuple[str, Path]]:
    """Return (display name, path) pairs for Hermes-managed SQLite databases."""
    # backup.py owns the canonical list of per-profile stores; reuse it.
    from hermes_cli.backup import _QUICK_STATE_FILES

    entries = [
        (name, hermes_home / name)
        for name in _QUICK_STATE_FILES
        if name.endswith(".db")
    ]
    # Non-default kanban boards each keep their own kanban.db.
    for board_db in sorted((hermes_home / "kanban" / "boards").glob("*/kanban.db")):
        entries.append((str(board_db.relative_to(hermes_home)), board_db))
    return entries


_SQLITE_HEADER_MAGIC = b"SQLite format 3\x00"


def _unreadable_reason(db_path: Path) -> str:
    """Explain why a database file could not be read, without opening it.

    ``read_header_bytes_preopen`` collapses every ``OSError`` into ``None``,
    but doctor's job is to say *which* problem it hit. ``stat()`` and
    ``access()`` answer that from directory metadata alone — neither takes a
    file descriptor, so neither can cancel the file's POSIX advisory locks.
    """
    try:
        db_path.stat()
    except OSError as exc:
        return str(exc)
    if not os.access(db_path, os.R_OK):
        return f"permission denied: {db_path}"
    return "file could not be read"


def _read_journal_mode(db_path: Path) -> tuple[str | None, str | None]:
    """Return (journal mode, error) from the file header without opening the database.

    Header byte 18 is 2 for WAL and 1 for a rollback journal. Opening the
    database through the SQLite engine — even read-only — creates -wal/-shm
    sidecar files, which a diagnostic must not do.

    The byte read is routed through ``read_header_bytes_preopen`` rather than
    a bare ``open()``: closing *any* descriptor for a database file cancels
    this process's POSIX advisory locks on it, so a raw read would drop the
    locks a live connection is holding (see ``hermes_cli.sqlite_safe_read``).
    ``run_doctor`` is also called in-process by the dashboard console, which
    holds live ``SessionDB`` connections. The helper refuses in that case and
    the mode is reported as unreadable instead.
    """
    from hermes_cli.sqlite_safe_read import (
        has_live_connection,
        read_header_bytes_preopen,
    )

    header = read_header_bytes_preopen(db_path, length=20)
    if header is None:
        if has_live_connection(db_path):
            return None, "database is open in this process"
        return None, _unreadable_reason(db_path)
    if len(header) == 0:
        return None, "file is empty"
    if len(header) < 20 or not header.startswith(_SQLITE_HEADER_MAGIC):
        return None, "file is not a database"
    if header[18] == 2:
        return "wal", None
    if header[18] == 1:
        return "rollback", None
    return None, f"unrecognized file-format version {header[18]}"


def _format_db_size(db_path: Path) -> str:
    # backup.py owns human-readable size formatting; reuse it (as with
    # _QUICK_STATE_FILES above) and keep only the stat-failure wrap here.
    from hermes_cli.backup import _format_size

    try:
        nbytes = db_path.stat().st_size
    except OSError:
        return "size unknown"
    return _format_size(nbytes)


def _report_database_journal_modes(
    hermes_home: Path | None = None,
    version_info: tuple[int, ...] | None = None,
) -> None:
    """List each database's journal mode; warn on WAL under a vulnerable SQLite."""
    from hermes_state import _wal_reset_repair_hint
    from hermes_state_wal import is_sqlite_wal_reset_vulnerable

    vulnerable = is_sqlite_wal_reset_vulnerable(version_info)
    home = hermes_home if hermes_home is not None else HERMES_HOME
    try:
        databases = _hermes_database_paths(home)
    except Exception as exc:
        check_warn(f"Could not list Hermes databases: {exc}")
        return
    exposed = []
    for name, path in databases:
        if not path.is_file():
            continue
        mode, error = _read_journal_mode(path)
        size = _format_db_size(path)
        if error is not None:
            if vulnerable:
                check_warn(
                    f"{name}: journal mode could not be read",
                    f"({error}; cannot rule out WAL exposure)",
                )
            else:
                check_info(f"{name}: journal mode could not be read ({error})")
        elif mode == "wal":
            if vulnerable:
                exposed.append(name)
                check_warn(
                    f"{name} is in WAL mode ({size})",
                    "(exposed to the WAL-reset bug until SQLite is upgraded)",
                )
            else:
                check_info(f"{name}: WAL journal mode ({size})")
        elif vulnerable:
            check_info(f"{name}: rollback journal mode ({size}, not exposed)")
        else:
            check_info(f"{name}: rollback journal mode ({size})")
    if exposed:
        check_info(f"To clear the exposure: {_wal_reset_repair_hint()}")


def _safe_which(cmd: str) -> str | None:
    """shutil.which wrapper resilient to platform monkeypatching in tests."""
    try:
        return shutil.which(cmd)
    except Exception:
        return None


def _termux_browser_setup_steps(node_installed: bool) -> list[str]:
    steps: list[str] = []
    step = 1
    if not node_installed:
        steps.append(f"{step}) pkg install nodejs")
        step += 1
    steps.append(f"{step}) npm install -g agent-browser")
    steps.append(f"{step + 1}) agent-browser install")
    return steps


def _termux_install_all_fallback_notes() -> list[str]:
    return [
        "Termux install profile: use .[termux-all] for broad compatibility (installer default on Termux).",
        "Matrix E2EE extra is excluded on Termux (python-olm currently fails to build).",
        "Local faster-whisper extra is excluded on Termux (ctranslate2/av build path unavailable).",
        "STT fallback: use Groq Whisper (set GROQ_API_KEY) or OpenAI Whisper (set VOICE_TOOLS_OPENAI_KEY).",
    ]


def _has_provider_env_config(content: str) -> bool:
    """Return True when ~/.hermes/.env contains provider auth/base URL settings."""
    return any(key in content for key in _PROVIDER_ENV_HINTS)


def _honcho_is_configured_for_doctor() -> bool:
    """Return True when Honcho is configured, even if this process has no active session."""
    try:
        from plugins.memory.honcho.client import HonchoClientConfig

        cfg = HonchoClientConfig.from_global_config()
        return bool(cfg.enabled and (cfg.api_key or cfg.base_url))
    except Exception:
        return False


def _is_kanban_worker_env_gate(item: dict) -> bool:
    """Return True when Kanban is unavailable only because this is not a worker process."""
    if item.get("name") != "kanban":
        return False
    if os.environ.get("HERMES_KANBAN_TASK"):
        return False

    tools = item.get("tools") or []
    return bool(tools) and all(str(tool).startswith("kanban_") for tool in tools)


def _doctor_tool_availability_detail(toolset: str) -> str:
    """Optional explanatory suffix for toolsets whose doctor status needs context."""
    if toolset == "kanban" and not os.environ.get("HERMES_KANBAN_TASK"):
        return "(runtime-gated; loaded only for dispatcher-spawned workers)"
    return ""


def _doctor_web_capability_rows() -> list[tuple[str, str, str]]:
    """Return doctor rows for web search/extract provider readiness (#78412).

    Each row is ``(status, label, detail)`` where *status* is ``ok`` or ``warn``.
    Uses the same active-provider resolvers as the tools, but reports readiness
    from ``is_available()`` so an explicitly selected but unconfigured backend
    does not look healthy.
    """
    rows: list[tuple[str, str, str]] = []
    try:
        from agent.web_search_registry import (
            get_active_extract_provider,
            get_active_search_provider,
        )
        from tools.web_tools import _ensure_web_plugins_loaded, _provider_is_ready

        # Doctor runs in a fresh process — bundled web providers register
        # during plugin discovery, which nothing has triggered yet here.
        # Without this the registry is empty and every row reads
        # "no provider selected or registered" (idempotent, cheap on rerun).
        _ensure_web_plugins_loaded()
    except Exception:
        return rows

    for capability, getter in (
        ("web search", get_active_search_provider),
        ("web extract", get_active_extract_provider),
    ):
        try:
            provider = getter()
        except Exception:
            provider = None
        if provider is None:
            rows.append(
                (
                    "warn",
                    capability,
                    "(no provider selected or registered)",
                )
            )
            continue
        name = getattr(provider, "name", None) or type(provider).__name__
        if _provider_is_ready(provider):
            rows.append(("ok", capability, f"({name})"))
        else:
            rows.append(
                (
                    "warn",
                    capability,
                    f"({name} selected; provider not configured)",
                )
            )
    return rows

def _apply_doctor_tool_availability_overrides(available: list[str], unavailable: list[dict]) -> tuple[list[str], list[dict]]:
    """Adjust runtime-gated tool availability for doctor diagnostics."""
    updated_available = list(available)
    updated_unavailable = []
    for item in unavailable:
        name = item.get("name")
        if _is_kanban_worker_env_gate(item):
            if "kanban" not in updated_available:
                updated_available.append("kanban")
            continue
        if name == "honcho" and _honcho_is_configured_for_doctor():
            if "honcho" not in updated_available:
                updated_available.append("honcho")
            continue
        updated_unavailable.append(item)
    return updated_available, updated_unavailable


def _has_healthy_oauth_fallback_for_apikey_provider(provider_label: str) -> bool:
    """Return True when a direct API-key probe failure is non-blocking.

    Some provider families support both a direct API-key path and a separate
    OAuth runtime path. When the OAuth path is already healthy, doctor should
    still show a failed API-key connectivity row, but it should not promote
    that direct-key problem into the final blocking summary.
    """
    normalized = (provider_label or "").strip().lower()
    if normalized == "minimax":
        try:
            from hermes_cli.auth import get_minimax_oauth_auth_status
            return bool((get_minimax_oauth_auth_status() or {}).get("logged_in"))
        except Exception:
            return False
    if normalized == "xai":
        try:
            from hermes_cli.auth import get_xai_oauth_auth_status
            return bool((get_xai_oauth_auth_status() or {}).get("logged_in"))
        except Exception:
            return False
    return False


def check_ok(text: str, detail: str = ""):
    print(f"  {color('✓', Colors.GREEN)} {text}" + (f" {color(detail, Colors.DIM)}" if detail else ""))

def check_warn(text: str, detail: str = ""):
    print(f"  {color('⚠', Colors.YELLOW)} {text}" + (f" {color(detail, Colors.DIM)}" if detail else ""))

def check_fail(text: str, detail: str = ""):
    print(f"  {color('✗', Colors.RED)} {text}" + (f" {color(detail, Colors.DIM)}" if detail else ""))

def check_info(text: str):
    print(f"    {color('→', Colors.CYAN)} {text}")


# ── state.db health/stats thresholds (advisory only — module constants,
# deliberately NOT config: doctor warnings are guidance, not policy) ──
STATE_DB_SIZE_WARN_BYTES = 1 * 1024 * 1024 * 1024   # 1 GiB logical size


# Shared byte formatter, aliased to the name this module's three rendering
# call sites already use.
from hermes_cli.sizefmt import format_bytes as _human_bytes


def _render_state_db_stats(stats: dict, holders=None) -> list:
    """Turn a collect_state_db_stats() dict into doctor output lines.

    Returns a list of ``(kind, text, detail)`` tuples where kind is one of
    'info' / 'warn'. Pure formatting — no I/O — so it is unit-testable
    without spawning the doctor CLI. Tolerates None in every field.
    """
    lines: list = []
    stats = stats or {}

    logical = stats.get("logical_size_bytes")
    wal = stats.get("wal_size_bytes")
    freelist = stats.get("freelist_count")

    size_bits = []
    if logical is not None:
        size_bits.append(f"logical size {_human_bytes(logical)}")
    if stats.get("page_count") is not None:
        size_bits.append(f"{stats['page_count']:,} pages")
    if freelist is not None:
        size_bits.append(f"{freelist:,} free")
    if wal is not None:
        size_bits.append(f"WAL {_human_bytes(wal)}")
    if size_bits:
        lines.append(("info", "state.db " + ", ".join(size_bits), ""))

    row_bits = []
    if stats.get("messages") is not None:
        row_bits.append(f"{stats['messages']:,} messages")
    if stats.get("sessions") is not None:
        row_bits.append(f"{stats['sessions']:,} sessions")
    if stats.get("journal_mode"):
        row_bits.append(f"journal_mode={stats['journal_mode']}")
    if holders is not None:
        row_bits.append(f"{holders} process(es) holding the DB open")
    if row_bits:
        lines.append(("info", ", ".join(row_bits), ""))

    fts = stats.get("fts_tables")
    if fts:
        present = [t for t, ok in fts.items() if ok]
        lines.append((
            "info",
            "FTS tables: " + (", ".join(present) if present else "none"),
            "",
        ))

    if stats.get("fts_repair_required"):
        failures = stats.get("fts_failure_count")
        count_text = f" after {failures} failure(s)" if failures is not None else ""
        last_error = stats.get("fts_last_error")
        detail = "run 'hermes sessions repair-search' offline with the gateway stopped"
        if last_error:
            detail += f"; last error: {last_error}"
        lines.append((
            "error",
            f"state.db search index is repair-required{count_text}",
            f"({detail})",
        ))

    deferral = stats.get("fts_rebuild_deferral")
    if isinstance(deferral, dict):
        attempts = deferral.get("attempts")
        pids = deferral.get("holder_pids") or []
        lines.append((
            "warn",
            f"state.db FTS repair is blocked after {attempts or '?'} "
            f"deferral(s) by PID(s) {pids or 'unknown'}",
            "(stop the listed processes, then run 'hermes sessions "
            "optimize-storage' with the gateway stopped)",
        ))

    # Advisory: oversized database. Suggest auto_prune, and — when the v23
    # FTS rebuild is pending OR the DB still carries the legacy inline
    # trigram layout (fts_storage_version marker absent) — the offline
    # optimize-storage pass that migrates/compacts the FTS indexes.
    if logical is not None and logical > STATE_DB_SIZE_WARN_BYTES:
        detail = (
            "consider enabling sessions.auto_prune in config.yaml "
            "to bound growth"
        )
        legacy_trigram = (
            fts is not None
            and fts.get("messages_fts_trigram")
            and stats.get("fts_storage_version") is None
        )
        if stats.get("fts_rebuild_pending") or legacy_trigram:
            detail += (
                "; run 'hermes sessions optimize-storage' offline "
                "(with the gateway stopped) to compact FTS storage"
            )
        lines.append((
            "warn",
            f"state.db is large ({_human_bytes(logical)})",
            f"({detail})",
        ))

    # WAL runaway is deliberately NOT warned here: the pre-existing WAL
    # check later in the state.db section already warns above 50 MB and
    # offers a checkpoint via --fix; a second warning at a higher threshold
    # would only duplicate it.

    return lines


def _section(title: str) -> None:
    """Print a doctor section banner: blank line + bold cyan ◆ title."""
    print()
    print(color(f"◆ {title}", Colors.CYAN, Colors.BOLD))


def _fail_and_issue(text: str, detail: str, fix: str, issues: list[str]) -> None:
    """Emit a check_fail and append the corresponding fix instruction."""
    check_fail(text, detail)
    issues.append(fix)


# Deprecated / legacy config keys still read for back-compat. Doctor surfaces
# them as non-failing warnings with the modern replacement — it does not
# auto-migrate or delete (migrations live in config.py version steps).
_DEPRECATED_CONFIG_KEYS: tuple[tuple[str, str, str], ...] = (
    # (section, key, replacement)
    ("display", "tool_progress_overrides", "display.platforms"),
    ("delegation", "max_async_children", "delegation.max_concurrent_children"),
)
from hermes_cli.doctor_tools import (
    _check_git_and_rg,
    _check_node_and_browser,
    _check_npm_audit,
    _check_terminal_backend,
    _check_tool_availability,
)
from hermes_cli.doctor_state import (
    _check_directory_structure,
    _check_memory_provider,
    _check_profiles,
    _check_skills_hub,
    _check_state_db,
)

_PROVIDER_ENV_HINTS = (
    "DEEPINFRA_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN",
    "OPENAI_BASE_URL", "NOUS_API_KEY", "GLM_API_KEY", "ZAI_API_KEY", "Z_AI_API_KEY", "KIMI_API_KEY",
    "KIMI_CN_API_KEY", "GMI_API_KEY", "FIREWORKS_API_KEY", "ACTUAL_API_KEY", "ACTUAL_BASE_URL", "MINIMAX_API_KEY",
    "MINIMAX_CN_API_KEY", "KILOCODE_API_KEY", "DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY", "HF_TOKEN",
    "AI_GATEWAY_API_KEY", "OPENCODE_ZEN_API_KEY", "OPENCODE_GO_API_KEY", "COMMANDCODE_API_KEY", "XIAOMI_API_KEY",
    "TOKENHUB_API_KEY", "TOKENPLAN_API_KEY",
)


@doctor_check()
def _check_auth_providers(should_fix: bool, f: Finding) -> None:
    """Refresh-free OAuth status snapshot (doctor must never trigger a token refresh)."""
    with warn_on_error("Auth provider status", "(could not check: {e})"):
        from hermes_cli.auth import get_nous_auth_status_local, get_codex_auth_status, get_minimax_oauth_auth_status
        _login_row("Nous Portal auth", get_nous_auth_status_local())
        # Native OAuth is Hermes' own device-code flow; the Codex CLI only imports existing ~/.codex/auth.json
        # tokens, so the hint sits under the Codex row (not as another provider's remedy).
        if not _login_row("OpenAI Codex auth", get_codex_auth_status(), show_error=True) and not _safe_which("codex"):
            check_info("codex CLI not installed (optional — only required to import tokens from an existing Codex CLI login)")
        minimax_status = get_minimax_oauth_auth_status()
        _login_row("MiniMax OAuth", minimax_status, f"(logged in, region={minimax_status.get('region', 'global')})")
    with warn_on_error(""):  # xAI OAuth separately, so an import failure cannot disrupt the rows already printed
        from hermes_cli.auth import get_xai_oauth_auth_status
        _login_row("xAI OAuth", get_xai_oauth_auth_status() or {}, show_error=True)


def _login_row(label: str, status: dict, ok_detail: str = "(logged in)", show_error: bool = False) -> bool:
    """ok/warn row for an OAuth status dict; with show_error, its ``error`` hint prints under a not-logged-in row."""
    logged_in = check_bool(status.get("logged_in"), (label, ok_detail), (label, "(not logged in)"))
    if not logged_in and show_error and status.get("error"):
        check_info(status["error"])
    return logged_in


@doctor_check()
def _check_api_connectivity(should_fix: bool, f: Finding) -> None:
    """Parallel HTTP/SDK probes for every configured provider; results printed in submission order."""
    probes = build_probes()
    # Single status line so users see something happening; ``\r`` clears it once results land.
    print(f"  {color(f'Running {len(probes)} connectivity checks in parallel…', Colors.DIM)}", end="", flush=True)
    results = run_probes(probes)
    print("\r" + " " * 70 + "\r", end="")
    for r in results:
        for glyph, label, detail in r.lines:
            print(f"  {glyph} {label}" + (f" {detail}" if detail else ""))
        if r.issues and not _has_healthy_oauth_fallback_for_apikey_provider(r.label):
            f.issues.extend(r.issues)


# Ordered (section title, check). None title = check prints its own header (or none); order is user-visible.
DOCTOR_CHECKS = (
    ('Security Advisories', _check_security_advisories), ('MCP Server Security', _check_mcp_security),
    ('Python Environment', _check_python_environment), ('SSL / CA Certificates', _check_certificates),
    ('Required Packages', _check_required_packages), ('Configuration Files', _check_env_file),
    (None, _check_config_file), (None, _check_config_drift),
    ('xAI Model Retirement (May 15, 2026)', _check_xai_retirement),
    ('Plugin import paths (removed Sep 14, 2026)', _check_plugin_compat), ('Auth Providers', _check_auth_providers),
    ('Directory Structure', _check_directory_structure), (None, _check_state_db),
    (None, _check_gateway_supervision), (None, _check_command_installation),
    ('External Tools', _check_git_and_rg), (None, _check_terminal_backend), (None, _check_node_and_browser),
    (None, _check_npm_audit), ('API Connectivity', _check_api_connectivity),
    ('Tool Availability', _check_tool_availability), ('Skills Hub', _check_skills_hub),
    ('Memory Provider', _check_memory_provider), (None, _check_profiles),
)


def _ack_advisory(ack_target: str) -> None:
    """`hermes doctor --ack <id>`: persist the ack and return without running diagnostics."""
    from hermes_cli.security_advisories import ADVISORIES, ack_advisory
    valid_ids = {a.id for a in ADVISORIES}
    if ack_target not in valid_ids:
        print(color(f"Unknown advisory ID: {ack_target!r}. Known IDs: {', '.join(sorted(valid_ids)) or '(none)'}", Colors.RED))
        sys.exit(2)
    if ack_advisory(ack_target):
        print(color(f"  ✓ Acknowledged advisory {ack_target}. It will no longer trigger startup banners.", Colors.GREEN))
    else:
        print(color(f"  ✗ Failed to persist ack for {ack_target}. Check ~/.hermes/config.yaml is writable.", Colors.RED))
        sys.exit(1)


def _print_summary(should_fix: bool, total: Finding) -> None:
    print()
    remaining = total.issues + total.manual_issues
    numbered = "".join(f"  {i}. {issue}\n" for i, issue in enumerate(remaining, 1))
    if should_fix and total.fixed > 0:
        print(color("─" * 60, Colors.GREEN))
        print(color(f"  Fixed {total.fixed} issue(s).", Colors.GREEN, Colors.BOLD), end="")
        print(color(f" {len(remaining)} issue(s) require manual intervention.", Colors.YELLOW, Colors.BOLD) if remaining else "")
        print()
        if remaining:
            print(numbered)
    elif remaining:
        print(color("─" * 60, Colors.YELLOW))
        print(color(f"  Found {len(remaining)} issue(s) to address:", Colors.YELLOW, Colors.BOLD))
        print()
        print(numbered)
        if not should_fix:
            print(color("  Tip: run 'hermes doctor --fix' to auto-fix what's possible.", Colors.DIM))
    else:
        print(color("─" * 60, Colors.GREEN))
        print(color("  All checks passed! 🎉", Colors.GREEN, Colors.BOLD))
    print()


def run_doctor(args):
    """Run diagnostic checks."""
    should_fix = getattr(args, 'fix', False)
    # Doctor runs from the interactive CLI, so CLI-gated tool checks (e.g. cronjob) see the same context.
    os.environ.setdefault("HERMES_INTERACTIVE", "1")
    if getattr(args, 'ack', None):
        return _ack_advisory(args.ack)
    print()
    for line in ("┌─────────────────────────────────────────────────────────┐",
                 "│                 🩺 Hermes Doctor                        │",
                 "└─────────────────────────────────────────────────────────┘"):
        print(color(line, Colors.CYAN))
    total = Finding()
    for title, check in DOCTOR_CHECKS:
        if title:
            _section(title)
        total.merge(check(should_fix))
    # Opt-in live probes run AFTER all static checks (`--live`: real network calls; bounded + read-only).
    with warn_on_error(""):
        from hermes_cli.doctor_live import maybe_run_live_checks
        maybe_run_live_checks(args, total.manual_issues)
    _print_summary(should_fix, total)


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.
from pathlib import Path  # noqa: F401,E402
import importlib.util  # noqa: F401,E402
import shutil  # noqa: F401,E402
import subprocess  # noqa: F401,E402

def check_fail(text: str, detail: str = ""):
    print(f"  {color('✗', Colors.RED)} {text}" + (f" {color(detail, Colors.DIM)}" if detail else ""))

def check_ok(text: str, detail: str = ""):
    print(f"  {color('✓', Colors.GREEN)} {text}" + (f" {color(detail, Colors.DIM)}" if detail else ""))

def check_warn(text: str, detail: str = ""):
    print(f"  {color('⚠', Colors.YELLOW)} {text}" + (f" {color(detail, Colors.DIM)}" if detail else ""))


_PLUGIN_COMPAT_LAZY = {
    'FTS_STORAGE_VERSION': ('hermes_state_common', 'FTS_STORAGE_VERSION'),
    'OPENROUTER_MODELS_URL': ('hermes_constants', 'OPENROUTER_MODELS_URL'),
    'STATE_DB_SIZE_WARN_BYTES': ('hermes_cli.doctor_state', 'STATE_DB_SIZE_WARN_BYTES'),
    'agent_browser_runnable': ('hermes_constants', 'agent_browser_runnable'),
    'base_url_host_matches': ('utils', 'base_url_host_matches'),
    'check_certificates': ('hermes_cli.doctor_platform', 'check_certificates'),
    'check_macos_full_disk_access': ('hermes_cli.doctor_platform', 'check_macos_full_disk_access'),
    'check_macos_tcc_anchor': ('hermes_cli.doctor_platform', 'check_macos_tcc_anchor'),
    'check_macos_tcc_grants': ('hermes_cli.doctor_platform', 'check_macos_tcc_grants'),
    'collect_deprecated_config_keys': ('hermes_cli.doctor_config', 'collect_deprecated_config_keys'),
    'collect_deprecated_env_vars': ('hermes_cli.doctor_config', 'collect_deprecated_env_vars'),
    'collect_relay_plugin_cutover_findings': ('hermes_cli.doctor_config', 'collect_relay_plugin_cutover_findings'),
    'describe_vercel_auth': ('hermes_cli.vercel_auth', 'describe_vercel_auth'),
    'detect_install_method': ('hermes_cli.config', 'detect_install_method'),
    'is_nix_install_method': ('hermes_cli.config', 'is_nix_install_method'),
    'managed_scope_check': ('hermes_cli.doctor_config', 'managed_scope_check'),
    'recommended_update_command_for_method': ('hermes_cli.config', 'recommended_update_command_for_method'),
    'report_deprecated_config_and_env': ('hermes_cli.doctor_config', 'report_deprecated_config_and_env'),
}


def __getattr__(name):  # PEP 562 — lazy so no import cycles
    target = _PLUGIN_COMPAT_LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib
    from hermes_cli.plugin_compat import warn_once
    warn_once(__name__, name, *target)
    return getattr(importlib.import_module(target[0]), target[1])
# ---- END PLUGIN-COMPAT ----
