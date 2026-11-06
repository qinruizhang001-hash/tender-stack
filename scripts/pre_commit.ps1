# -*- coding: utf-8 -*-
# pre-commit 逻辑：必跑自检；改了工作台/前端再加跑冒烟。
# 由 .git/hooks/pre-commit 调用，也可以手动执行。
$ErrorActionPreference = "Stop"
$env:PYTHONIOENCODING = "utf-8"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

$root = Split-Path -Parent $PSScriptRoot
$changed = & git -C $root diff --cached --name-only --diff-filter=ACMR 2>$null
if (-not $changed) {
    Write-Host "没有暂存改动，跳过检查" -ForegroundColor DarkGray
    exit 0
}

# 纯文档改动不值得跑全套测试
$docsOnly = @(".md", ".txt", ".csv", ".png", ".jpg", ".jpeg", ".pdf", ".svg")
$code = @($changed | Where-Object {
    $ext = [System.IO.Path]::GetExtension($_).ToLower()
    $docsOnly -notcontains $ext
})
if (-not $code) {
    Write-Host "只改了文档，跳过自检" -ForegroundColor DarkGray
    exit 0
}

# 动了工作台或前端，才需要真起服务 + 真开浏览器
$uiChanged = @($changed | Where-Object {
    $_ -like "workbench/*" -or $_ -like "scripts/smoke*" -or $_ -like "scripts/check_frontend*"
})

Write-Host "== pre-commit：自检 ==" -ForegroundColor Cyan
# 冒烟由下一步单独跑，这里的 pytest 先跳过它，避免起两次服务
$env:DSH_SKIP_SMOKE = "1"
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root "scripts\check.ps1")
if ($LASTEXITCODE -ne 0) {
    Write-Host "自检未通过，提交已阻止" -ForegroundColor Red
    exit 1
}

if ($uiChanged.Count -gt 0) {
    Write-Host "== pre-commit：改了界面，跑一次冒烟 ==" -ForegroundColor Cyan
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root "scripts\smoke.ps1") -SkipBrowser
    if ($LASTEXITCODE -ne 0) {
        Write-Host "冒烟未通过，提交已阻止" -ForegroundColor Red
        exit 1
    }
}

Write-Host "pre-commit 通过" -ForegroundColor Green
exit 0
