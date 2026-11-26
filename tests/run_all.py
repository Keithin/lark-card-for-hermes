#!/usr/bin/env python3
"""Offline compatibility gate: real Hermes modules, fake Feishu transport, disposable plugin copy.

Set LCH_HERMES_DIR to a clean official checkout. No credentials or running gateway needed.
"""
from __future__ import annotations
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str]) -> int:
    wanted = {name.lstrip("-") for name in argv[1:]}
    selected = [p.name for p in sorted(ROOT.glob("tests/check_*.py"))
                if not wanted or p.stem.removeprefix("check_") in wanted]
    if not selected:
        print(f"no checks matched {sorted(wanted)}")
        return 2
    hermes = Path(os.environ.get("LCH_HERMES_DIR", str(Path.home() / ".hermes/hermes-agent"))).resolve()
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("HERMES_SESSION_") and key not in {"HERMES_PLATFORM", "HERMES_HOME"}}
    env["LCH_HERMES_DIR"] = str(hermes)
    with tempfile.TemporaryDirectory(prefix="lch-checks-") as directory:
        sandbox = Path(directory)
        plugin = sandbox / "home/plugins/lark-card-for-hermes"
        shutil.copytree(ROOT, plugin, ignore=shutil.ignore_patterns(
            ".git", "state", "config.json", "__pycache__", ".hermes-home", ".venv"))
        (plugin / "config.json").write_text('{"card":{"reading_preset":"focused","tool_activity":"summary"}}', encoding="utf-8")
        env["HERMES_HOME"] = str(sandbox / "home")
        failures = []
        for name in selected:
            proc = subprocess.run([sys.executable, str(plugin / "tests" / name)], cwd=plugin,
                                  env=env, capture_output=True, text=True)
            tail = (proc.stdout or proc.stderr).strip().splitlines()[-1:] or [""]
            status = "PASS" if proc.returncode == 0 else "FAIL"
            print(f"{status:4} {Path(name).stem:16} {tail[0][:110]}", flush=True)
            if proc.returncode:
                failures.append((name, proc.returncode, proc.stdout, proc.stderr))
        for name, code, out, err in failures:
            print(f"\n--- {name} exited {code}\n{err[-2000:]}{out[-2000:]}")
        if failures:
            return 1
        print(f"\nALL PASS ({len(selected)} checks)")
        return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
