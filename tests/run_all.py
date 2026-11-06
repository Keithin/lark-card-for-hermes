#!/usr/bin/env python3
"""插件自带的离线判据入口：不联网、不需要网关、不碰真实飞书。

用法（在插件仓库根目录）：
    python tests/run_all.py            # 全部
    python tests/run_all.py parity …   # 只跑指定几项

每个 check_*.py 都是独立进程（它们会 import 插件并建立自己的假 transport），
一项失败不影响其他项，退出码非零即失败。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = sorted(ROOT.glob("tests/check_*.py"))


def main(argv: list[str]) -> int:
    wanted = {name.lstrip("-") for name in argv[1:]}
    selected = [p for p in TESTS if not wanted or p.stem.removeprefix("check_") in wanted]
    if not selected:
        print(f"no checks matched {sorted(wanted)}")
        return 2
    env = dict(os.environ)
    # 判据不写用户 profile：状态目录指向临时目录（插件自身也只在显式需要时才建 state/）
    env.setdefault("HERMES_HOME", str(ROOT / "tests" / ".hermes-home"))
    failures = []
    for path in selected:
        proc = subprocess.run([sys.executable, str(path)], cwd=ROOT, env=env,
                              capture_output=True, text=True)
        tail = (proc.stdout or proc.stderr).strip().splitlines()[-1:] or [""]
        status = "PASS" if proc.returncode == 0 else "FAIL"
        print(f"{status:4} {path.stem:16} {tail[0][:110]}")
        if proc.returncode != 0:
            failures.append((path.name, proc.returncode, proc.stdout, proc.stderr))
    if failures:
        for name, code, out, err in failures:
            print(f"\n--- {name} exited {code}\n{err[-2000:]}{out[-2000:]}")
        return 1
    print(f"\nALL PASS ({len(selected)} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
