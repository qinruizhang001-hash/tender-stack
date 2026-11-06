# -*- coding: utf-8 -*-
# pre-push：条款金标门禁 + 完整冒烟（含浏览器）。
# 提交时只跑 HTTP 冒烟，浏览器那 15 秒不该花在每次 commit 上。
$ErrorActionPreference = "Stop"
$env:PYTHONIOENCODING = "utf-8"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
# 别让 check.ps1 留下的跳过标记漏进来
$env:DSH_SKIP_SMOKE = ""

$root = Split-Path -Parent $PSScriptRoot

Write-Host "== pre-push：条款金标门禁 ==" -ForegroundColor Cyan
& (Join-Path $root ".venv\Scripts\python.exe") (Join-Path $root "scripts\gold_gate.py")
if ($LASTEXITCODE -ne 0) {
    Write-Host "条款金标 recall 低于阈值，推送已阻止（确认要推可加 --no-verify）" -ForegroundColor Red
    exit 1
}

Write-Host "== pre-push：完整冒烟 ==" -ForegroundColor Cyan
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root "scripts\smoke.ps1")
if ($LASTEXITCODE -ne 0) {
    Write-Host "冒烟未通过，推送已阻止（确认要推可加 --no-verify）" -ForegroundColor Red
    exit 1
}

Write-Host "pre-push 通过" -ForegroundColor Green
exit 0
