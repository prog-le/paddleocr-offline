# =============================================================================
# Windows 停止脚本
# 用法：powershell -ExecutionPolicy Bypass -File scripts\stop_windows.ps1
# =============================================================================
#requires -Version 5.1
[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$PidFile = Join-Path $Root "data\server.pid"

if (-not (Test-Path $PidFile)) {
    Write-Host "[信息] 没有找到 data\server.pid，可能不是以 -Daemon 方式启动" -ForegroundColor Yellow
    exit 0
}

$ServerPid = (Get-Content $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
if (-not $ServerPid) {
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    Write-Host "[信息] PID 文件为空，已清理" -ForegroundColor Yellow
    exit 0
}

$proc = Get-Process -Id $ServerPid -ErrorAction SilentlyContinue
if ($proc) {
    Stop-Process -Id $ServerPid -Force
    Write-Host "[通过] 已停止 PID $ServerPid" -ForegroundColor Green
} else {
    Write-Host "[信息] PID $ServerPid 已不存在" -ForegroundColor Yellow
}
Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
