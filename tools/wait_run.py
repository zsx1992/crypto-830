# -*- coding: utf-8 -*-
"""轮询单个 GitHub Actions run 直到完成, 然后下载日志 zip。
用法: python tools/wait_run.py <run_id> [--out <zip路径>]
"""
import sys
import os
import time
import json
import subprocess

RUN_ID = sys.argv[1]
OUT = sys.argv[2] if len(sys.argv) > 2 else os.path.join(
    os.environ.get("TEMP", "/tmp"), "runlogs", f"run_{RUN_ID}.zip")

token = ""
with open(os.path.expanduser("~/.git-credentials"), encoding="utf-8") as f:
    for ln in f:
        if "github.com" in ln:
            token = ln.strip().rsplit(":", 1)[1].split("@")[0]
            break

API = "https://api.github.com/repos/zsx1992/crypto-830/actions/runs"


def gh(url):
    r = subprocess.run(
        ["curl", "-s", "-H", f"Authorization: Bearer {token}", url],
        capture_output=True, text=True)
    return json.loads(r.stdout or "{}")


def main():
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    deadline = time.time() + 30 * 60  # 30 分钟上限
    while time.time() < deadline:
        d = gh(f"{API}/{RUN_ID}")
        st = d.get("status")
        print(f"[{time.strftime('%H:%M:%S')}] status={st} conclusion={d.get('conclusion')}", flush=True)
        if st == "completed":
            break
        time.sleep(45)
    else:
        print("TIMEOUT waiting run", flush=True)
        sys.exit(1)

    concl = d.get("conclusion")
    print(f"run {RUN_ID} conclusion={concl}", flush=True)
    if concl != "success":
        print("run failed, skip log download", flush=True)
        sys.exit(0)

    # 下载日志 zip
    subprocess.run(["curl", "-sL", "-H", f"Authorization: Bearer {token}",
                    "-o", OUT, f"{API}/{RUN_ID}/logs"], check=True)
    print(f"logs saved: {OUT} ({os.path.getsize(OUT)} bytes)", flush=True)


if __name__ == "__main__":
    main()
