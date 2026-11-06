# -*- coding: utf-8 -*-
# 安装 git hooks：把 scripts/hooks/* 复制到 .git/hooks/
# 用法：powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_hooks.ps1
#       ... -File scripts\install_hooks.ps1 -Force   # 覆盖已存在的 hook
param(
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$gitDir = Join-Path $root ".git"
if (-not (Test-Path $gitDir)) {
    Write-Host "不是 git 仓库：$root" -ForegroundColor Red
    exit 1
}

$hooksDir = Join-Path $gitDir "hooks"
New-Item -ItemType Directory -Force -Path $hooksDir | Out-Null

$source = Join-Path $root "scripts\hooks"
Get-ChildItem $source -File | ForEach-Object {
    $target = Join-Path $hooksDir $_.Name
    if ((Test-Path $target) -and (-not $Force)) {
        # 别覆盖用户自己写的 hook，提示它手动合并
        $existing = Get-Content $target -Raw -ErrorAction SilentlyContinue
        if ($existing -notmatch "scripts/pre_commit.ps1") {
            Write-Host "已存在自定义 hook，跳过：$($_.Name)（用 -Force 覆盖）" -ForegroundColor Yellow
            return
        }
    }
    # hook 由 sh 执行，必须是 LF，不能带 BOM
    $text = [System.IO.File]::ReadAllText($_.FullName, [System.Text.Encoding]::UTF8)
    $text = $text -replace "`r`n", "`n"
    [System.IO.File]::WriteAllText($target, $text, (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "已安装：.git/hooks/$($_.Name)" -ForegroundColor Green
}

Write-Host "完成。以后 git commit 会自动跑 scripts/pre_commit.ps1" -ForegroundColor Green
Write-Host "临时跳过：git commit --no-verify" -ForegroundColor DarkGray
