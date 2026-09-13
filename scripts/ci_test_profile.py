#!/usr/bin/env python3
"""Single dependency-profile authority for canonical CI test environments."""
from __future__ import annotations

import argparse
import shutil
import subprocess

CI_TEST_EXTRAS = (
    "all",
    "dev",
    "anthropic",
    "mistral",
    "fal",
    "modal",
    "daytona",
    "hindsight",
    "parallel-web",
)


def sync_command(python: str) -> list[str]:
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("uv is unavailable; cannot sync the canonical CI test profile")
    command = [uv, "sync", "--locked", "--python", python]
    for extra in CI_TEST_EXTRAS:
        command.extend(("--extra", extra))
    return command


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("sync",))
    parser.add_argument("--python", default="3.11")
    args = parser.parse_args()
    return subprocess.run(sync_command(args.python), check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
