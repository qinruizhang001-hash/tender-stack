#!/usr/bin/env python3
"""启用并发布 GitHub Pages —— ai-bid-document 可视化发布页。

零依赖（仅标准库），兼容 Python 3.8+。

用法
----
    # 1) 只诊断，不改任何东西（强烈建议先跑这个）
    python tools/activate_pages.py --check --token-file .token

    # 2) 启用 Pages（源码 = GitHub Actions）+ 触发 pages.yml + 等待上线
    python tools/activate_pages.py --token-file .token

    # 3) 只触发一次构建（Pages 已启用过）
    python tools/activate_pages.py --deploy-only --token-file .token

    # 4) 临时绕过系统代理（代理失效时用）
    python tools/activate_pages.py --check --no-proxy --token-file .token

凭据
----
推荐 `--token-file`（从文件读，不进程列表泄露、不进 shell history）。
也支持 `--token` 或环境变量 `GH_TOKEN` / `GITHUB_TOKEN`。

需要的权限（二选一）
--------------------
* Fine-grained PAT，仅授权 Jackon-Li/ai-bid-document：
    Contents: Read and write      # 读仓库、触发 workflow
    Pages:    Read and write      # 创建/更新 Pages 站点
    Administration: Read and write # 改仓库 Pages 设置
    Metadata: Read（自动带上）
* Classic PAT：勾 `repo` + `workflow`

退出码
------
0 成功 / 1 接口或构建失败 / 2 凭据或参数问题
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request

API = "https://api.github.com"
DEFAULT_OWNER = "Jackon-Li"
DEFAULT_REPO = "ai-bid-document"
DEFAULT_WORKFLOW = "pages.yml"

# 网络不稳时的重试策略（只对连接层错误重试，不对 4xx 重试）
NET_RETRIES = 4
NET_BACKOFF = [2, 4, 8, 12]


# --------------------------------------------------------------------------
# HTTP 层
# --------------------------------------------------------------------------
class ApiError(Exception):
    """接口调用失败，携带状态码与 GitHub 返回的 message。"""

    def __init__(self, status, message, hint=""):
        self.status = status
        self.message = message
        self.hint = hint
        super().__init__(f"HTTP {status}: {message}")


def build_opener(no_proxy: bool) -> urllib.request.OpenerDirector:
    handlers = []
    if no_proxy:
        # 空 ProxyHandler 覆盖环境变量里的 http_proxy/https_proxy
        handlers.append(urllib.request.ProxyHandler({}))
    handlers.append(urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    return urllib.request.build_opener(*handlers)


def api(opener, method: str, path: str, token: str, body=None, *, raw=False):
    """调用 GitHub REST API。返回 (status, headers, payload)。"""
    url = path if path.startswith("http") else API + path
    data = None
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "ai-bid-document-activate-pages",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    last_err = None
    for attempt in range(NET_RETRIES):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with opener.open(req, timeout=30) as resp:
                text = resp.read().decode("utf-8", "replace")
                return resp.status, dict(resp.headers), (text if raw else _json(text))
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            # 4xx 不重试，直接带出语义
            raise ApiError(e.code, _message(text), _hint(e.code, path)) from None
        except (urllib.error.URLError, ssl.SSLError, TimeoutError, OSError) as e:
            last_err = e
            if attempt < NET_RETRIES - 1:
                wait = NET_BACKOFF[min(attempt, len(NET_BACKOFF) - 1)]
                print(f"  · 连接失败（{e}），{wait}s 后重试 {attempt + 2}/{NET_RETRIES}", flush=True)
                time.sleep(wait)
    raise ApiError(0, f"网络不可达：{last_err}",
                   "检查网络或代理；代理挂了可加 --no-proxy 直连")


def _json(text):
    if not text.strip():
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"_raw": text}


def _message(text: str) -> str:
    payload = _json(text)
    msg = payload.get("message") if isinstance(payload, dict) else None
    if msg:
        errors = payload.get("errors")
        if errors:
            msg += " | " + "; ".join(
                str(e.get("message", e)) if isinstance(e, dict) else str(e) for e in errors
            )
        return msg
    return text[:200] or "（无返回内容）"


def _hint(status: int, path: str) -> str:
    if status == 401:
        return "Token 无效或已过期 —— 重新生成后写入 token 文件"
    if status == 403:
        return ("权限不足。Fine-grained PAT 需 Contents/Pages/Administration 均为 "
                "Read and write；Classic PAT 需勾 repo(+workflow)")
    if status == 404:
        if path.endswith("/pages"):
            return "Pages 尚未启用（对 /pages 的 404 就是未启用，不是仓库不存在）"
        return "仓库不存在，或 Token 未被授权访问该仓库（fine-grained 需显式 Selected repositories）"
    if status == 409:
        return "Pages 已存在该配置或构建冲突，可改用 --deploy-only 只触发构建"
    return ""


# --------------------------------------------------------------------------
# 业务步骤
# --------------------------------------------------------------------------
def step_repo(opener, token, owner, repo):
    st, hd, d = api(opener, "GET", f"/repos/{owner}/{repo}", token)
    perms = d.get("permissions") or {}
    print(f"[1/5] 仓库可访问：{owner}/{repo}")
    print(f"      默认分支 = {d.get('default_branch')}｜private = {d.get('private')}")
    if perms:
        print(f"      我的权限 = {', '.join(k for k, v in perms.items() if v) or '只读'}")
    scopes = hd.get("X-Oauth-Scopes") or hd.get("x-oauth-scopes") or ""
    if scopes:
        print(f"      Token scopes = {scopes}")
        if "repo" not in scopes and "public_repo" not in scopes:
            print("      ⚠ Token 缺 repo 权限，后续步骤大概率 403")
    return d.get("default_branch") or "main", bool(perms.get("admin"))


def step_pages_state(opener, token, owner, repo):
    try:
        st, hd, d = api(opener, "GET", f"/repos/{owner}/{repo}/pages", token)
        return d
    except ApiError as e:
        if e.status == 404:
            return None
        raise


def step_enable(opener, token, owner, repo, branch):
    """幂等：未启用则 POST 创建，已启用则 PUT 校正为 Actions 构建。"""
    existing = step_pages_state(opener, token, owner, repo)
    if existing:
        st, _, d = api(opener, "PUT", f"/repos/{owner}/{repo}/pages", token,
                       {"build_type": "workflow"})
        print(f"[2/5] Pages 已存在，已校正构建方式 = {d.get('build_type')}")
        return d
    st, _, d = api(opener, "POST", f"/repos/{owner}/{repo}/pages", token,
                   {"build_type": "workflow"})
    print(f"[2/5] Pages 已创建：build_type = {d.get('build_type', 'workflow')}，"
          f"html_url = {d.get('html_url', '-')}")
    return d


def step_dispatch(opener, token, owner, repo, workflow, branch):
    try:
        st, _, _ = api(opener, "POST",
                       f"/repos/{owner}/{repo}/actions/workflows/{workflow}/dispatches",
                       token, {"ref": branch})
        print(f"[3/5] 已触发 workflow `{workflow}`（ref={branch}），HTTP {st}")
        return True
    except ApiError as e:
        if e.status == 404:
            print(f"[3/5] ⚠ 找不到 workflow `{workflow}`，跳过触发。"
                  f"确认 .github/workflows/{workflow} 已推送")
            return False
        raise


def step_watch(opener, token, owner, repo, workflow, timeout=180):
    """轮询最近一次 run，直到结束或超时。"""
    deadline = time.time() + timeout
    seen_id = None
    while time.time() < deadline:
        st, _, d = api(opener, "GET",
                       f"/repos/{owner}/{repo}/actions/workflows/{workflow}/runs?per_page=1",
                       token)
        runs = d.get("workflow_runs") or []
        if not runs:
            print("[4/5] 还没有 run 记录，等待…", flush=True)
            time.sleep(8)
            continue
        run = runs[0]
        seen_id = run["id"]
        status, concl = run.get("status"), run.get("conclusion")
        print(f"[4/5] run #{run['run_number']} status={status} conclusion={concl}", flush=True)
        if status == "completed":
            print(f"      {run.get('html_url')}")
            if concl != "success":
                print(f"      ⚠ 构建未成功（{concl}），检查 workflow 日志；"
                      f"若提示 Pages 未开启，说明第 2 步未生效")
            return concl == "success"
        time.sleep(10)
    print(f"[4/5] 等待超时（{timeout}s），run {seen_id} 可能仍在排队")
    return False


def step_verify(opener, url, token, rounds=6, wait=15):
    """验证站点真的可访问（Pages 首次构建后需要一点时间生效）。"""
    for i in range(rounds):
        try:
            st, _, text = api(opener, "GET", url, token, raw=True)
            body = text if isinstance(text, str) else ""
            if st == 200 and len(body) > 1000:
                print(f"[5/5] 站点可访问 ✓  HTTP {st}，{len(body)} 字节")
                return True
            print(f"[5/5] HTTP {st}，内容 {len(body)} 字节，稍后重试…", flush=True)
        except ApiError as e:
            print(f"[5/5] {e}，稍后重试…", flush=True)
        time.sleep(wait)
    return False


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------
def read_token(args) -> str:
    if args.token_file:
        try:
            with open(args.token_file, "r", encoding="utf-8") as f:
                return f.read().strip()
        except OSError as e:
            print(f"读不到 token 文件：{e}", file=sys.stderr)
            raise SystemExit(2)
    if args.token:
        return args.token.strip()
    for var in ("GH_TOKEN", "GITHUB_TOKEN", "GITHUB_PAT"):
        if os.environ.get(var):
            return os.environ[var].strip()
    print("缺凭据：用 --token-file / --token，或设置 GH_TOKEN 环境变量", file=sys.stderr)
    raise SystemExit(2)


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    ap = argparse.ArgumentParser(
        description="启用并发布 GitHub Pages（可视化发布页）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：python tools/activate_pages.py --check --token-file .token",
    )
    ap.add_argument("--owner", default=DEFAULT_OWNER)
    ap.add_argument("--repo", default=DEFAULT_REPO)
    ap.add_argument("--workflow", default=DEFAULT_WORKFLOW, help="Pages 工作流文件名")
    ap.add_argument("--token", help="PAT（不安全，优先 --token-file）")
    ap.add_argument("--token-file", help="存放 PAT 的文本文件")
    ap.add_argument("--no-proxy", action="store_true", help="忽略 http(s)_proxy，直连")
    ap.add_argument("--check", action="store_true", help="只诊断，不做任何修改")
    ap.add_argument("--deploy-only", action="store_true", help="跳过启用，只触发构建")
    ap.add_argument("--timeout", type=int, default=180, help="等待构建的秒数")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="构建失败也返回 0（默认返回 1）")
    args = ap.parse_args(argv)

    token = read_token(args)
    opener = build_opener(args.no_proxy)
    print(f"目标：{args.owner}/{args.repo}｜workflow={args.workflow}｜"
          f"代理={'直连' if args.no_proxy else '按环境变量'}\n")

    try:
        branch, is_admin = step_repo(opener, token, args.owner, args.repo)

        if args.check:
            pages = step_pages_state(opener, token, args.owner, args.repo)
            if pages:
                print(f"[2/5] Pages 已启用：build_type={pages.get('build_type')} "
                      f"status={pages.get('status')} url={pages.get('html_url')}")
            else:
                print("[2/5] Pages 未启用 → 需要 admin 权限创建；本次为 --check，未做修改")
            try:
                st, _, d = api(opener, "GET",
                               f"/repos/{args.owner}/{args.repo}/actions/workflows/"
                               f"{args.workflow}/runs?per_page=1", token)
                runs = d.get("workflow_runs") or []
                if runs:
                    r = runs[0]
                    print(f"[3/5] 最近 run #{r['run_number']} "
                          f"status={r['status']} conclusion={r.get('conclusion')}")
                else:
                    print("[3/5] 尚无 run 记录")
            except ApiError as e:
                print(f"[3/5] 查 workflow 失败：{e}")
            print(f"[4/5] admin 权限：{'有' if is_admin else '无（无法启用 Pages）'}")
            print("[5/5] 诊断结束，未做任何修改")
            return 0

        if not args.deploy_only:
            step_enable(opener, token, args.owner, args.repo, branch)
        else:
            print("[2/5] --deploy-only，跳过启用")

        if not step_dispatch(opener, token, args.owner, args.repo, args.workflow, branch):
            return 1 if not args.allow_dirty else 0

        ok_build = step_watch(opener, token, args.owner, args.repo, args.workflow,
                              args.timeout)
        url = f"https://{args.owner.lower()}.github.io/{args.repo}/"
        ok_live = step_verify(opener, url, token) if ok_build else False

        print("\n" + "=" * 62)
        if ok_live:
            print(f"完成 ✓ 发布页已上线：{url}")
            print("可把该链接放进 README 顶部与 Release 正文的按钮位。")
            return 0
        print("未完全成功 ✗")
        print(f"  构建成功 = {ok_build}｜站点可访问 = {ok_live}")
        print(f"  手工兜底：仓库 Settings → Pages → Source 选 'GitHub Actions'，"
              f"再重跑 Actions 里的 Pages 工作流")
        return 0 if args.allow_dirty else 1

    except ApiError as e:
        print(f"\n失败：{e}", file=sys.stderr)
        if e.hint:
            print(f"提示：{e.hint}", file=sys.stderr)
        return 2 if e.status in (401, 403, 404) and "Token" in (e.hint or "") else 1
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
