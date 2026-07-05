"""Read-only Hermes agent log health reporting.

Scans recent Hermes log files and prints a concise, redacted health report.
Intended for script-only cron jobs inside each agent environment.
"""

from __future__ import annotations

import argparse
import os
import re
import socket
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

DEFAULT_LOGS = ("errors.log", "agent.log", "gateway.log", "proxy.log")
TZ = ZoneInfo("Europe/Zurich")

SEVERITY_PATTERNS = (
    (
        "red",
        re.compile(
            r"traceback|uncaught|fatal|panic|segmentation fault|disk full|no space left|"
            r"permission denied|auth.*fail|\b401\b|unauthorized|forbidden|service.*failed|"
            r"failed to start|last_error",
            re.I,
        ),
    ),
    (
        "red",
        re.compile(
            r"honcho.*(unreachable|error|failed)|database.*(locked|error|failed)|"
            r"redis.*(error|failed)",
            re.I,
        ),
    ),
    (
        "yellow",
        re.compile(
            r"exception|\berror\b|failed|failure|upstream_unreachable|"
            r"upstream_invalid_response|delivery_error",
            re.I,
        ),
    ),
    (
        "yellow",
        re.compile(
            r"timeout|timed out|upstream_timeout|\b429\b|rate limit|quota|unhealthy|"
            r"fallback|gemini|compression failed|mcp.*error|tool.*error|warning|warn",
            re.I,
        ),
    ),
)

REDACTIONS = (
    (
        re.compile(
            r"(?i)(api[_-]?key|token|secret|password|authorization|bearer)\s*[:=]\s*[^\s,;]+"
        ),
        r"\1=[REDACTED]",
    ),
    (re.compile(r"(?i)bearer\s+[a-z0-9._\-]+"), "Bearer [REDACTED]"),
    (re.compile(r"(?i)(key-|sk-|xox[baprs]-)[a-z0-9_\-]{12,}"), "[REDACTED_TOKEN]"),
    (re.compile(r"([?&](?:token|key|auth|password|secret)=)[^\s&]+", re.I), r"\1[REDACTED]"),
)

TS_PATTERNS = (
    re.compile(r"(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)"),
    re.compile(
        r"(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
        r"(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)"
    ),
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only Hermes agent log health report")
    parser.add_argument("--agent", default=os.environ.get("HERMES_AGENT_NAME") or socket.gethostname())
    parser.add_argument("--hours", type=float, default=12.5)
    parser.add_argument("--log-dir", default=str(Path.home() / ".hermes" / "logs"))
    parser.add_argument("--max-examples", type=int, default=8)
    parser.add_argument("--quiet-ok", action="store_true", help="Print nothing when no findings")
    return parser.parse_args(argv)


def redact(text: str) -> str:
    text = text.strip()
    for pattern, replacement in REDACTIONS:
        text = pattern.sub(replacement, text)
    if len(text) > 260:
        text = text[:257] + "..."
    return text


def parse_ts(line: str, tz: ZoneInfo = TZ) -> datetime | None:
    for pattern in TS_PATTERNS:
        match = pattern.search(line)
        if not match:
            continue
        raw = match.group("ts").replace(",", ".")
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        return dt.astimezone(tz)
    return None


def classify(line: str) -> str | None:
    for severity, pattern in SEVERITY_PATTERNS:
        if pattern.search(line):
            return severity
    return None


def iter_log_lines(path: Path):
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return
    except Exception as exc:  # pragma: no cover - depends on filesystem failure
        yield None, f"LOG_READ_ERROR {path.name}: {type(exc).__name__}: {exc}"
        return
    for line in text.splitlines():
        yield parse_ts(line), line


def signature(line: str) -> str:
    sig = redact(line).lower()
    sig = re.sub(r"\d{4}-\d{2}-\d{2}[ t]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?", "<ts>", sig)
    sig = re.sub(r"\b\d+\b", "<n>", sig)
    sig = re.sub(r"\s+", " ", sig).strip()
    return sig[:160]


def render_report(
    *,
    agent: str,
    log_dir: Path,
    hours: float,
    max_examples: int,
    quiet_ok: bool = False,
    now: datetime | None = None,
) -> str:
    now = (now or datetime.now(TZ)).astimezone(TZ)
    since = now - timedelta(hours=hours)
    counts: defaultdict[tuple[str, str], int] = defaultdict(int)
    files_seen: list[str] = []
    examples: list[tuple[str, str, str]] = []
    newest: datetime | None = None
    seen_signatures: set[tuple[str, str, str]] = set()

    for name in DEFAULT_LOGS:
        path = log_dir / name
        if not path.exists():
            continue
        files_seen.append(name)
        for ts, line in iter_log_lines(path):
            if ts and ts < since:
                continue
            if ts and (newest is None or ts > newest):
                newest = ts
            severity = classify(line)
            if not severity:
                continue
            sig = signature(line)
            key = (severity, name, sig)
            if key in seen_signatures:
                continue
            seen_signatures.add(key)
            counts[(severity, name)] += 1
            if len(examples) < max_examples:
                examples.append((severity, name, redact(line)))

    red = sum(value for (severity, _), value in counts.items() if severity == "red")
    yellow = sum(value for (severity, _), value in counts.items() if severity == "yellow")
    worst = "red" if red else "yellow" if yellow else "green"

    if worst == "green":
        if quiet_ok:
            return ""
        return (
            f"🟢 Agent log health OK — {agent} — {now.strftime('%Y-%m-%d %H:%M %Z')}\n"
            f"Időablak: utolsó {hours:g} óra. Vizsgált logok: "
            f"{', '.join(files_seen) if files_seen else 'nincs logfájl'}."
        )

    icon = "🔴" if worst == "red" else "🟡"
    lines = [
        f"{icon} Agent log health report — {agent} — {now.strftime('%Y-%m-%d %H:%M %Z')}",
        f"Időablak: utolsó {hours:g} óra",
        f"Összesítés: kritikus={red}, figyelmeztetés={yellow}",
    ]
    if newest:
        lines.append(f"Legfrissebb érintett logidő: {newest.strftime('%Y-%m-%d %H:%M %Z')}")
    lines.append("")

    by_file: defaultdict[str, dict[str, int]] = defaultdict(lambda: {"red": 0, "yellow": 0})
    for (severity, name), count in counts.items():
        by_file[name][severity] += count
    for name in sorted(by_file):
        item = by_file[name]
        lines.append(f"- {name}: kritikus={item['red']}, figyelmeztetés={item['yellow']}")

    lines.extend(["", "Példák redaktálva:"])
    for severity, name, line in examples:
        mark = "🔴" if severity == "red" else "🟡"
        lines.append(f"{mark} {name}: {line}")
    lines.extend(["", "Read-only report. Nem történt restart, javítás vagy konfigurációmódosítás."])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = render_report(
        agent=args.agent,
        log_dir=Path(args.log_dir).expanduser(),
        hours=args.hours,
        max_examples=args.max_examples,
        quiet_ok=args.quiet_ok,
    )
    if report:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
