# -*- coding: utf-8 -*-
# 本地一致性检查：Python 语法 + 前端语法 + 单元测试 + 规则自测
# 用法（Windows PowerShell 5.1 也可）：
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\check.ps1
#   powershell ... -File scripts\check.ps1 -SkipTests
param(
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
# 控制台默认是 GBK，Python 输出中文会乱码；统一成 UTF-8
$env:PYTHONIOENCODING = "utf-8"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

Write-Host "== 1/5 Python 语法检查 ==" -ForegroundColor Cyan
& $py -m compileall -q (Join-Path $root "adapter") (Join-Path $root "tests") (Join-Path $root "scripts") (Join-Path $root "workbench")
if ($LASTEXITCODE -ne 0) {
    Write-Host "Python 语法检查失败" -ForegroundColor Red
    exit 1
}
Write-Host "语法 OK" -ForegroundColor Green

Write-Host "== 2/5 前端脚本语法检查 ==" -ForegroundColor Cyan
& $py (Join-Path $root "scripts\check_frontend.py")
if ($LASTEXITCODE -ne 0) {
    Write-Host "前端脚本语法错误（页面会白屏）" -ForegroundColor Red
    exit 1
}

if ($SkipTests) { exit 0 }

Write-Host "== 3/5 单元测试 ==" -ForegroundColor Cyan
& $py -m pytest (Join-Path $root "tests") -q
if ($LASTEXITCODE -ne 0) {
    Write-Host "测试失败" -ForegroundColor Red
    exit 1
}

Write-Host "== 4/5 数值比对自测 ==" -ForegroundColor Cyan
& $py (Join-Path $root "adapter\numeric_check.py")
if ($LASTEXITCODE -ne 0) {
    Write-Host "数值自测失败" -ForegroundColor Red
    exit 1
}

Write-Host "== 5/5 时间比对自测 ==" -ForegroundColor Cyan
& $py (Join-Path $root "adapter\time_check.py")
if ($LASTEXITCODE -ne 0) {
    Write-Host "时间自测失败" -ForegroundColor Red
    exit 1
}

Write-Host "全部通过" -ForegroundColor Green
exit 0
