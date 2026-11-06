# =============================================================================
# Windows x64 离线安装（PowerShell 5.1+ / Windows 10 1803+）
#
# 用法（在解压后的项目根目录）：
#   powershell -ExecutionPolicy Bypass -File scripts\install_windows.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\install_windows.ps1 -SystemPython
#   powershell -ExecutionPolicy Bypass -File scripts\install_windows.ps1 -Python C:\Python312\python.exe
#
# 前置（都在离线包里）：
#   offline\python\windows-amd64\cpython-3.12.*-x86_64-pc-windows-msvc-install_only.tar.gz
#   offline\wheels\windows-amd64\*.whl
#   models\paddlex_home\official_models\...
#
# 说明：
#   * 不解压 zip 直接用资源管理器打开压缩包运行会失败，请先完整解压；
#   * Paddle 3.3.1 需要 Microsoft Visual C++ 2015-2022 Redistributable (x64)；
#   * 全程 --no-index，不会访问网络。
# =============================================================================
#requires -Version 5.1
[CmdletBinding()]
param(
    [string]$Python = "",
    [switch]$SystemPython,
    [string]$Venv = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Target = "windows-amd64"
$Wheels = Join-Path $Root "offline\wheels\$Target"
$RuntimeDir = Join-Path $Root "offline\python\$Target"
$ReqFile = Join-Path $Root "requirements-windows.txt"
$VenvDir = if ($Venv) { $Venv } else { Join-Path $Root ".venv" }
$VenvPy = Join-Path $VenvDir "Scripts\python.exe"

function Fail([string]$Msg) {
    Write-Host "[错误] $Msg" -ForegroundColor Red
    exit 1
}

function Info([string]$Msg) { Write-Host "[信息] $Msg" -ForegroundColor Cyan }
function Ok([string]$Msg) { Write-Host "[通过] $Msg" -ForegroundColor Green }
function Warn([string]$Msg) { Write-Host "[警告] $Msg" -ForegroundColor Yellow }

function Check-Exit([string]$What) {
    if ($LASTEXITCODE -ne 0) { Fail "$What（退出码 $LASTEXITCODE）" }
}

Write-Host ""
Write-Host "===== PaddleX OCR Web · Windows 离线安装 =====" -ForegroundColor White
Info "项目目录: $Root"

if (-not [Environment]::Is64BitOperatingSystem) {
    Fail "只支持 64 位 Windows（Paddle 没有 win32 轮子）"
}
Ok "Windows x64"

if (-not (Test-Path $ReqFile)) { Fail "缺少 $ReqFile" }
if (-not (Test-Path $Wheels)) { Fail "缺少 $Wheels（请先执行 download_wheels.sh --os windows --arch amd64）" }
$WheelCount = @(Get-ChildItem $Wheels -Filter *.whl).Count
if ($WheelCount -lt 1) { Fail "$Wheels 中没有 wheel 文件" }
$PaddleWhl = @(Get-ChildItem $Wheels -Filter *.whl | Where-Object { $_.Name -like "paddlepaddle-*win_amd64*" })
if ($PaddleWhl.Count -lt 1) {
    Fail "$Wheels 中缺少 Windows 版 paddlepaddle wheel"
}
Ok "离线 wheel: $WheelCount 个"

# ---------------------------------------------------------------- 选择解释器
function Resolve-BasePython {
    if ($Python) {
        if (-not (Test-Path $Python)) { Fail "-Python 指定的解释器不存在：$Python" }
        return (Resolve-Path $Python).Path
    }
    if (-not $SystemPython) {
        $tar = Get-ChildItem $RuntimeDir -Filter "*.tar.gz" -ErrorAction SilentlyContinue |
            Sort-Object Name -Descending | Select-Object -First 1
        if ($tar) {
            $RuntimeRoot = Join-Path $Root ".runtime\python"
            $RuntimePy = Join-Path $RuntimeRoot "python.exe"
            if (-not (Test-Path $RuntimePy)) {
                $tarExe = Get-Command tar.exe -ErrorAction SilentlyContinue
                if (-not $tarExe) { Fail "系统缺少 tar.exe（Windows 10 1803+ 自带；也可解压 $($tar.Name) 到 .runtime）" }
                $tmp = Join-Path $Root ".runtime\.unpack"
                if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
                New-Item -ItemType Directory -Path $tmp -Force | Out-Null
                Info "解包随包 Python: $($tar.Name)"
                & tar.exe -xzf $tar.FullName -C $tmp
                Check-Exit "解包运行时失败"
                $src = Join-Path $tmp "python"
                if (-not (Test-Path (Join-Path $src "python.exe"))) { Fail "运行时压缩包结构异常（找不到 python\python.exe）" }
                if (Test-Path $RuntimeRoot) { Remove-Item $RuntimeRoot -Recurse -Force }
                New-Item -ItemType Directory -Path (Split-Path $RuntimeRoot -Parent) -Force | Out-Null
                Move-Item $src $RuntimeRoot
                Remove-Item $tmp -Recurse -Force
            }
            Ok "随包 Python: $RuntimePy"
            return $RuntimePy
        }
        Warn "离线包里没有 Windows 运行时，改用系统 Python"
    }
    $sys = Get-Command python.exe -ErrorAction SilentlyContinue
    if (-not $sys) { Fail "找不到 python.exe。请安装 Python 3.10-3.13 x64，或把运行时压缩包放进 $RuntimeDir" }
    return $sys.Source
}

$BasePy = Resolve-BasePython
$VerOut = & $BasePy -c "import sys;print('%d.%d' % sys.version_info[:2])"
Check-Exit "读取 Python 版本失败"
$Ver = $VerOut.Trim()
switch ($Ver) {
    "3.10" { }
    "3.11" { }
    "3.12" { }
    "3.13" { }
    default { Fail "Python $Ver 不受支持（wheel 为 cp310~cp313，推荐随包 3.12）" }
}
Ok "Python $Ver"

# ---------------------------------------------------------------- venv + 安装
if (-not (Test-Path $VenvPy)) {
    Info "创建虚拟环境: $VenvDir"
    & $BasePy -m venv $VenvDir
    Check-Exit "创建 venv 失败"
}
# 随包运行时自带 pip；若被裁剪则用 ensurepip 补齐
& $VenvPy -m pip --version *> $null
if ($LASTEXITCODE -ne 0) {
    Info "venv 缺少 pip，执行 ensurepip"
    & $VenvPy -m ensurepip --upgrade
    Check-Exit "ensurepip 失败"
}

Info "离线安装依赖（--no-index，仅使用 $Wheels）"
& $VenvPy -m pip install --no-index --find-links $Wheels --disable-pip-version-check -r $ReqFile
Check-Exit "依赖安装失败（常见原因：wheel 与 Python $Ver 不匹配、缺少 VC++ 运行库或 wheel 不完整）"
Ok "依赖安装完成"

# ---------------------------------------------------------------- 校验
$Models = Join-Path $Root "models\paddlex_home\official_models"
if (-not (Test-Path $Models) -or -not (Get-ChildItem $Models -ErrorAction SilentlyContinue)) {
    Warn "模型目录为空：$Models（打包时可用 --skip-models，之后需手动放入模型）"
} else {
    Ok "模型目录: $Models"
}

$VerifyCode = @'
import sys
sys.path.insert(0, sys.argv[1])
mods = ["fastapi", "uvicorn", "yaml", "PIL", "docx", "openpyxl", "psutil", "pypdfium2", "fitz"]
for m in mods:
    __import__(m)
import paddle
print("paddle", paddle.__version__)
from backend.main import create_app
print("backend ok")
'@
$VerifyCode | & $VenvPy - $Root
if ($LASTEXITCODE -ne 0) {
    $vc = Get-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64" -ErrorAction SilentlyContinue
    if (-not $vc) {
        Warn "检测到模块导入失败，且未发现 VC++ 运行库。请安装 Microsoft Visual C++ 2015-2022 Redistributable (x64) 后重试。"
    }
    Fail "依赖自检失败（退出码 $LASTEXITCODE）"
}
Ok "依赖自检通过"

# 运行时来源记录（与 Linux/macOS 的 .runtime_env 对齐）
$runtimeLines = @(
    "python_bin=$BasePy",
    "python_version=$Ver",
    "venv=$VenvDir",
    "target=$Target",
    "arch=amd64",
    "device=cpu",
    "installed_at=$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
)
$runtimeLines | Set-Content -Path (Join-Path $Root ".runtime_env") -Encoding UTF8

Write-Host ""
Ok "安装完成"
Write-Host @"

下一步（PowerShell）：
  .\scripts\start_windows.ps1                 # 前台启动
  .\scripts\start_windows.ps1 -Daemon         # 后台启动，日志 logs\server.log
  http://<本机IP>:端口/                       # 端口取 config\config.yaml 的 server.port

注意：
  * 若双击 .ps1 无反应，请用：
      powershell -ExecutionPolicy Bypass -File scripts\start_windows.ps1
  * Windows 防火墙需放行服务端口。
"@ -ForegroundColor Gray
