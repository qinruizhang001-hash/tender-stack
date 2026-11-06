# -*- coding: utf-8 -*-
"""push_via_api.py — 当 git push 被网络阻断时，用 GitHub REST API 建一个提交。

背景：这台机器上 `git push` 到 github.com 会被重置，但 api.github.com 可达。
因此用 curl（走系统 TLS）逐文件上传 blob，再拼 tree/commit/ref。

不做历史：把当前工作区的已跟踪文件作为一个提交推上去。
用法：GH_TOKEN=... python scripts/push_via_api.py owner/repo "commit message"
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import time

API = "https://api.github.com"
CURL = "curl.exe" if os.name == "nt" else "curl"


def request(method: str, path: str, payload: dict | None = None, token: str = "", retries: int = 3) -> dict:
    headers = [
        "-H", f"Authorization: token {token}",
        "-H", "Accept: application/vnd.github+json",
        "-H", "Content-Type: application/json",
        "-H", "User-Agent: tender-stack-publisher",
    ]
    body_file = None
    command = [CURL, "-sS", "--max-time", "120", "-X", method, *headers, API + path]
    if payload is not None:
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(payload, handle, ensure_ascii=False)
        handle.close()
        body_file = handle.name
        command += ["--data-binary", f"@{body_file}"]

    last_error = ""
    for attempt in range(1, retries + 1):
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
        text = result.stdout.strip()
        if result.returncode == 0 and text.startswith("{"):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                last_error = text[:200]
        else:
            last_error = (result.stderr or text)[:200]
        time.sleep(1.5 * attempt)

    raise RuntimeError(f"{method} {path} 失败：{last_error}")
    if body_file:
        os.unlink(body_file)


def tracked_files(root: str) -> list[str]:
    # 中文路径默认会被 git 转义成 \346\216... ，用 -z 拿原始路径
    output = subprocess.run(
        ["git", "-C", root, "-c", "core.quotepath=false", "ls-files", "-z"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout
    return [item for item in output.split("\0") if item.strip()]


def main() -> None:
    repo = sys.argv[1]
    message = sys.argv[2] if len(sys.argv) > 2 else "chore: initial import"
    token = os.environ.get("GH_TOKEN") or ""
    if not token:
        raise SystemExit("需要 GH_TOKEN")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    files = tracked_files(root)
    print(f"files: {len(files)}", flush=True)

    parent = ""
    try:
        refs = request("GET", f"/repos/{repo}/git/refs/heads/main", token=token)
        parent = refs.get("object", {}).get("sha", "")
    except RuntimeError:
        pass

    tree: list[dict] = []
    for index, relative in enumerate(files, start=1):
        absolute = os.path.join(root, relative.replace("/", os.sep))
        with open(absolute, "rb") as handle:
            content = handle.read()
        blob = request(
            "POST",
            f"/repos/{repo}/git/blobs",
            {"content": base64.b64encode(content).decode(), "encoding": "base64"},
            token,
        )
        tree.append({"path": relative, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        if index % 20 == 0:
            print(f"  uploaded {index}/{len(files)}", flush=True)

    tree_result = request("POST", f"/repos/{repo}/git/trees", {"tree": tree}, token)
    commit_payload: dict = {"message": message, "tree": tree_result["sha"]}
    if parent:
        commit_payload["parents"] = [parent]
    commit = request("POST", f"/repos/{repo}/git/commits", commit_payload, token)

    if parent:
        request("PATCH", f"/repos/{repo}/git/refs/heads/main", {"sha": commit["sha"], "force": True}, token)
    else:
        request("POST", f"/repos/{repo}/git/refs", {"ref": "refs/heads/main", "sha": commit["sha"]}, token)

    print(json.dumps({"commit": commit["sha"], "files": len(tree)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
