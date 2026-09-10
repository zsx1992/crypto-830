# -*- coding: utf-8 -*-
"""等 diag-fetch run#7/8/9 完成, 下载 case-klines artifact 解压到 data_diag/"""
import os, io, re, sys, time, json, zipfile
import urllib.request

RUNS = [34420951470, 34420953060, 34420954721]
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def token():
    for line in open(os.path.expanduser("~/.git-credentials"),
                     encoding="utf-8"):
        if "github.com" in line:
            return re.search(r"https://zsx1992:([^@]*)@", line).group(1)
    raise SystemExit("no token")


def api(url):
    req = urllib.request.Request(url, headers={
        "Authorization": f"token {token()}", "User-Agent": "diag"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


opener = urllib.request.build_opener(NoRedirect)


def main():
    t = token()
    for i in range(60):
        done = True
        for rid in RUNS:
            r = api(f"https://api.github.com/repos/zsx1992/crypto-830/actions/runs/{rid}")
            print(f"[{time.strftime('%H:%M:%S')}] run {rid} "
                  f"status={r.get('status')} concl={r.get('conclusion')}",
                  flush=True)
            if r.get("status") != "completed":
                done = False
        if done:
            break
        time.sleep(20)
    # 下载 artifacts
    for rid in RUNS:
        arts = api(f"https://api.github.com/repos/zsx1992/crypto-830/actions/runs/{rid}/artifacts")["artifacts"]
        for a in arts:
            if a["name"] != "case-klines":
                continue
            try:
                opener.open(urllib.request.Request(
                    a["archive_download_url"],
                    headers={"Authorization": f"token {t}"}), timeout=30)
            except urllib.error.HTTPError as e:
                loc = e.headers.get("Location")
            data = urllib.request.urlopen(
                urllib.request.Request(loc), timeout=120).read()
            z = zipfile.ZipFile(io.BytesIO(data))
            for n in z.namelist():
                if n.endswith(".csv"):
                    target = os.path.join(_ROOT, "data_diag",
                                          os.path.basename(os.path.dirname(n)),
                                          os.path.basename(n))
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with open(target, "wb") as f:
                        f.write(z.read(n))
            print(f"run {rid}: 解压完成 -> data_diag/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
