# -*- coding: utf-8 -*-
# 端到端冒烟：起一个真实服务 → 跑 HTTP 冒烟 → 用浏览器确认页面真的渲染 → 关掉
# 用法：
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\smoke.ps1
#   powershell ... -File scripts\smoke.ps1 -SkipBrowser
param(
    [int]$Port = 8399,
    [switch]$SkipBrowser
)

$ErrorActionPreference = "Stop"
$env:PYTHONIOENCODING = "utf-8"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

# 冒烟只需要工作台本身，不要让 keeper 去拉起 rag-tender 与 worker
$env:WORKBENCH_AUTOSTART = "0"
# pre-commit 会设这个变量让 check.ps1 跳过冒烟；这里是冒烟本身，必须清掉
$env:DSH_SKIP_SMOKE = ""

Write-Host "== 启动临时服务 :$Port ==" -ForegroundColor Cyan
$proc = Start-Process -FilePath $py `
    -ArgumentList "-m", "uvicorn", "workbench.server:app", "--host", "127.0.0.1", "--port", $Port, "--log-level", "warning" `
    -WorkingDirectory $root -PassThru -WindowStyle Hidden

try {
    $ready = $false
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Milliseconds 500
        try {
            $response = Invoke-WebRequest "http://127.0.0.1:$Port/api/state" -UseBasicParsing -TimeoutSec 2
            if ($response.StatusCode -eq 200) { $ready = $true; break }
        } catch {}
        if ($proc.HasExited) { break }
    }
    if (-not $ready) {
        Write-Host "服务没有起来" -ForegroundColor Red
        exit 1
    }

    Write-Host "== HTTP 冒烟 ==" -ForegroundColor Cyan
    & $py -m pytest (Join-Path $root "tests\test_smoke.py") -q
    if ($LASTEXITCODE -ne 0) {
        Write-Host "HTTP 冒烟失败" -ForegroundColor Red
        exit 1
    }

    if ($SkipBrowser) {
        Write-Host "已跳过浏览器冒烟" -ForegroundColor Yellow
        exit 0
    }

    $ego = Get-Command ego-browser -ErrorAction SilentlyContinue
    if (-not $ego) {
        Write-Host "没有 ego-browser，跳过浏览器冒烟" -ForegroundColor Yellow
        exit 0
    }

    Write-Host "== 浏览器冒烟 ==" -ForegroundColor Cyan
    $script = Join-Path $root "scripts\smoke_ui.mjs"
    $url = "file:///" + ($script -replace '\\', '/')
    $env:SMOKE_BASE = "http://127.0.0.1:$Port"
    & ego-browser nodejs -e "await import('$url')"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "浏览器冒烟失败" -ForegroundColor Red
        exit 1
    }

    Write-Host "冒烟通过" -ForegroundColor Green
    exit 0
}
finally {
    if ($proc -and -not $proc.HasExited) {
        Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
        Write-Host "已关闭临时服务" -ForegroundColor DarkGray
    }
}
