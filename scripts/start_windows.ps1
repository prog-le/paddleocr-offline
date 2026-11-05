# =============================================================================
# Windows 启动脚本（PowerShell 5.1+）
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File scripts\start_windows.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\start_windows.ps1 -Daemon
#   powershell -ExecutionPolicy Bypass -File scripts\start_windows.ps1 -Port 9000 -Mock
#
# 说明：
#   * 环境变量与 scripts/start.sh 保持一致（离线模型、禁用联网检查）；
#   * -Daemon 会写 data\server.pid，日志 logs\server.log，用 stop_windows.ps1 停止；
#   * 不依赖 nohup/kill 等 Unix 工具。
# =============================================================================
#requires -Version 5.1
[CmdletBinding()]
param(
    [int]$Port = 0,
    [string]$BindHost = "",
    [switch]$Daemon,
    [switch]$Mock
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$VenvPy = Join-Path $Root ".venv\Scripts\python.exe"
$Config = Join-Path $Root "config\config.yaml"
$PidFile = Join-Path $Root "data\server.pid"

function Fail([string]$Msg) {
    Write-Host "[错误] $Msg" -ForegroundColor Red
    exit 1
}

if (-not (Test-Path $VenvPy)) {
    Fail "尚未安装：请先执行 powershell -ExecutionPolicy Bypass -File scripts\install_windows.ps1"
}

# 从 config.yaml 读端口/监听地址（用 venv 里的 PyYAML，避免重复解析逻辑）
if ($Port -le 0 -or -not $BindHost) {
    $cfgJson = & $VenvPy -c "import json,sys,yaml;c=yaml.safe_load(open(sys.argv[1],encoding='utf-8')) or {};s=c.get('server') or {};print(json.dumps({'host':s.get('host','0.0.0.0'),'port':int(s.get('port',8000))}))" $Config
    if ($LASTEXITCODE -ne 0 -or -not $cfgJson) { Fail "读取 config\config.yaml 失败" }
    $cfg = $cfgJson | ConvertFrom-Json
    if ($Port -le 0) { $Port = [int]$cfg.port }
    if (-not $BindHost) { $BindHost = [string]$cfg.host }
}

$ModelHome = Join-Path $Root "models\paddlex_home"
$Models = Join-Path $ModelHome "official_models"
$LogDir = Join-Path $Root "logs"
$DataDir = Join-Path $Root "data"
New-Item -ItemType Directory -Path $LogDir, $DataDir, (Join-Path $Root "models") -Force | Out-Null

# 环境变量：离线优先，避免首次启动尝试联网检查/下载
$env:PADDLE_PDX_CACHE_HOME = $ModelHome
$env:PADDLE_PDX_MODEL_SOURCE = if ($env:PADDLE_PDX_MODEL_SOURCE) { $env:PADDLE_PDX_MODEL_SOURCE } else { "BOS" }
$env:PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK = "True"
$env:PADDLE_PDX_DISABLE_TELEMETRY = "True"
$env:HF_HUB_OFFLINE = "1"
$env:TRANSFORMERS_OFFLINE = "1"
$env:MODELSCOPE_OFFLINE = "1"
$env:PYTHONUNBUFFERED = "1"
$env:PYTHONUTF8 = "1"
$env:PADDLE_PDX_CACHE_HOME = $ModelHome
if ($Mock) { $env:PADDLEX_OCR_MOCK = "1" }

if (-not $Mock -and (-not (Test-Path $Models) -or @(Get-ChildItem $Models -ErrorAction SilentlyContinue).Count -eq 0)) {
    Write-Host "[警告] 模型目录为空，服务能启动但识别任务会失败：$Models" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "===== PaddleX OCR Web · Windows =====" -ForegroundColor White
Write-Host "[信息] 监听: ${BindHost}:${Port}"
Write-Host "[信息] 模型: $Models"
Write-Host "[信息] 日志: $(Join-Path $LogDir 'server.log')"

$uvArgs = @(
    "-m", "uvicorn", "backend.main:app",
    "--host", $BindHost,
    "--port", "$Port",
    "--workers", "1",
    "--no-access-log",
    "--timeout-keep-alive", "75"
)

Set-Location $Root

if ($Daemon) {
    if (Test-Path $PidFile) {
        $old = Get-Content $PidFile -ErrorAction SilentlyContinue
        if ($old -and (Get-Process -Id $old -ErrorAction SilentlyContinue)) {
            Fail "服务已在运行（PID $old），请先执行 scripts\stop_windows.ps1"
        }
    }
    $stdout = Join-Path $LogDir "server.log"
    $stderr = Join-Path $LogDir "server.err.log"
    $proc = Start-Process -FilePath $VenvPy -ArgumentList $uvArgs -WorkingDirectory $Root `
        -RedirectStandardOutput $stdout -RedirectStandardError $stderr `
        -WindowStyle Hidden -PassThru
    $proc.Id | Set-Content -Path $PidFile -Encoding ASCII
    Start-Sleep -Seconds 3
    if (Get-Process -Id $proc.Id -ErrorAction SilentlyContinue) {
        Write-Host "[通过] 已启动，PID $($proc.Id)" -ForegroundColor Green
        Write-Host "[信息] 访问: http://<本机IP>:$Port    后台: http://<本机IP>:$Port/admin"
        Write-Host "[信息] 日志: $stdout / $stderr"
    } else {
        Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
        Fail "启动失败，请查看 $stderr"
    }
} else {
    Write-Host "[信息] 前台运行，Ctrl+C 停止" -ForegroundColor Cyan
    & $VenvPy @uvArgs
}
