#!/usr/bin/env bash
# =============================================================================
# 公共环境变量与工具函数（被其它脚本 source）
# 兼容：Linux ARM64 (aarch64) / x86_64 (amd64)、macOS（便于本地验证）
# =============================================================================
set -o pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PROJECT_ROOT

CONFIG_FILE="${PADDLEX_OCR_CONFIG:-$PROJECT_ROOT/config/config.yaml}"
export CONFIG_FILE
export PYTHONUNBUFFERED=1
export PIP_DISABLE_PIP_VERSION_CHECK=1

# ------------------------------------------------------------------ 架构识别
detect_arch() {
  local m
  m="$(uname -m)"
  case "$m" in
    x86_64|amd64)   echo "amd64" ;;
    aarch64|arm64)  echo "arm64" ;;
    *)              echo "unknown:$m" ;;
  esac
}

# ------------------------------------------------------------------ 系统识别
detect_os() {
  case "$(uname -s)" in
    Linux)                echo "linux" ;;
    Darwin)               echo "macos" ;;
    MINGW*|MSYS*|CYGWIN*) echo "windows" ;;
    *)                    echo "unknown" ;;
  esac
}

# ------------------------------------------------------------------ 部署目标矩阵
# 目标 id 约定：<os>-<arch>；Linux GPU 额外带 -gpu 后缀。
# 支持的组合（其余组合在 Paddle/PaddleX 上游就没有可用轮子，脚本会明确拒绝）：
#   linux-amd64 / linux-arm64       Paddle 3.4.0 CPU（Paddle 官方源）
#   linux-amd64-gpu                 Paddle 3.4.0 GPU（仅 linux x86_64）
#   macos-arm64                     Paddle 3.3.1 CPU（PyPI；Apple Silicon）
#   windows-amd64                   Paddle 3.3.1 CPU（PyPI）
SUPPORTED_TARGETS=(
  linux-amd64 linux-arm64 linux-amd64-gpu
  macos-arm64 windows-amd64
)
CPU_TARGETS=(linux-amd64 linux-arm64 macos-arm64 windows-amd64)

# 当前构建/运行机器对应的目标 id（不支持的系统返回 unknown）
target_id() {
  local os arch
  os="$(detect_os)"
  arch="$(detect_arch)"
  case "$arch" in amd64|arm64) ;; *) echo "unknown"; return 0 ;; esac
  case "$os" in
    linux)   echo "linux-${arch}" ;;
    macos)   echo "macos-${arch}" ;;
    windows) echo "windows-${arch}" ;;
    *)       echo "unknown" ;;
  esac
}

target_os() {
  case "$1" in
    linux-*)   echo "linux" ;;
    macos-*)   echo "macos" ;;
    windows-*) echo "windows" ;;
    *)         echo "unknown" ;;
  esac
}

target_arch_id() {
  case "$1" in
    linux-amd64-gpu) echo "amd64" ;;
    linux-arm64-gpu) echo "arm64" ;;
    *-amd64)         echo "amd64" ;;
    *-arm64)         echo "arm64" ;;
    *)               echo "unknown" ;;
  esac
}

target_device() {
  case "$1" in
    *-gpu) echo "gpu" ;;
    *)     echo "cpu" ;;
  esac
}

target_supported() {
  local t
  for t in "${SUPPORTED_TARGETS[@]}"; do
    [ "$t" = "$1" ] && return 0
  done
  return 1
}

target_support_note() {
  case "$1" in
    macos-amd64)
      echo "macOS Intel 没有 paddlepaddle 轮子（PyPI 只发布 macosx_11_0_arm64）；请改用 macos-arm64 或 Docker" ;;
    windows-arm64)
      echo "Windows ARM64 没有 paddlepaddle 轮子；请改用 windows-amd64" ;;
    linux-arm64-gpu|linux-arm64-gpu-*)
      echo "Paddle 官方 GPU 轮子只有 linux x86_64；ARM + NVIDIA 请用 NGC 镜像" ;;
    macos-arm64-gpu|windows-amd64-gpu)
      echo "Paddle 没有 macOS/Windows GPU 轮子；GPU 仅支持 linux-amd64" ;;
    *)
      echo "未知目标：$1（可选：${SUPPORTED_TARGETS[*]}）" ;;
  esac
}

target_requirements() {
  case "$(target_os "$1")" in
    linux)   echo "requirements.txt" ;;
    macos)   echo "requirements-macos.txt" ;;
    windows) echo "requirements-windows.txt" ;;
    *)       return 1 ;;
  esac
}

# wheel / 运行时目录：Linux 沿用已有的 offline/wheels/<arch> 布局，
# macOS / Windows 使用 offline/<kind>/<target>，避免不同平台混装。
target_wheels_dir() {
  case "$1" in
    linux-amd64)     echo "$PROJECT_ROOT/offline/wheels/amd64" ;;
    linux-arm64)     echo "$PROJECT_ROOT/offline/wheels/arm64" ;;
    linux-amd64-gpu) echo "$PROJECT_ROOT/offline/wheels-gpu/amd64" ;;
    *)               echo "$PROJECT_ROOT/offline/wheels/$1" ;;
  esac
}

target_runtime_dir() {
  case "$1" in
    linux-amd64|linux-amd64-gpu) echo "$PROJECT_ROOT/offline/python/amd64" ;;
    linux-arm64)                 echo "$PROJECT_ROOT/offline/python/arm64" ;;
    *)                           echo "$PROJECT_ROOT/offline/python/$1" ;;
  esac
}

target_runtime_triple() {
  case "$1" in
    linux-amd64|linux-amd64-gpu) echo "x86_64-unknown-linux-gnu" ;;
    linux-arm64)   echo "aarch64-unknown-linux-gnu" ;;
    macos-arm64)   echo "aarch64-apple-darwin" ;;
    macos-amd64)   echo "x86_64-apple-darwin" ;;
    windows-amd64) echo "x86_64-pc-windows-msvc" ;;
    *)             return 1 ;;
  esac
}

# GNU 风格架构名；$1 可传 amd64/arm64，缺省用当前架构
wheel_arch() {
  local a="${1:-$(detect_arch)}"
  case "$a" in
    amd64) echo "x86_64" ;;
    arm64) echo "aarch64" ;;
    *)     echo "unknown" ;;
  esac
}

# Docker 平台名
docker_platform() {
  case "$(detect_arch)" in
    amd64) echo "linux/amd64" ;;
    arm64) echo "linux/arm64" ;;
    *)     echo "unsupported" ;;
  esac
}


# ------------------------------------------------------------------ 输出
_c_red=$'\033[31m'; _c_green=$'\033[32m'; _c_yellow=$'\033[33m'; _c_cyan=$'\033[36m'; _c_off=$'\033[0m'
[ -t 1 ] || { _c_red=""; _c_green=""; _c_yellow=""; _c_cyan=""; _c_off=""; }

info()  { printf '%s[信息]%s %s\n' "$_c_cyan" "$_c_off" "$*"; }
ok()    { printf '%s[成功]%s %s\n' "$_c_green" "$_c_off" "$*"; }
warn()  { printf '%s[警告]%s %s\n' "$_c_yellow" "$_c_off" "$*" >&2; }
die()   { printf '%s[失败]%s %s\n' "$_c_red" "$_c_off" "$*" >&2; exit 1; }
step()  { printf '\n%s==> %s%s\n' "$_c_cyan" "$*" "$_c_off"; }

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "缺少命令：$1（$2）"
}

# 从 config.yaml 读取一个点分键（优先用 venv 的 python，其次系统 python）
config_get() {
  local key="$1" default="${2:-}"
  local py
  py="$(resolve_python || true)"
  [ -n "$py" ] || { echo "$default"; return; }
  "$py" - "$CONFIG_FILE" "$key" "$default" <<'PY' 2>/dev/null || echo "$default"
import sys
try:
    import yaml
except Exception:
    print(sys.argv[3]); raise SystemExit
path, key, default = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    data = yaml.safe_load(open(path, encoding="utf-8")) or {}
except Exception:
    print(default); raise SystemExit
cur = data
for part in key.split("."):
    if not isinstance(cur, dict) or part not in cur:
        print(default); raise SystemExit
    cur = cur[part]
print(cur)
PY
}

# ------------------------------------------------------------------ Python 运行时
# 随包 CPython 运行时的解包位置（由 install_offline.sh 解包，目标机无需系统 python）
runtime_dir() { echo "${PADDLEX_OCR_RUNTIME_DIR:-$PROJECT_ROOT/.runtime/python}"; }

# 随包运行时的解释器路径；不存在返回 1
runtime_python() {
  local d c
  d="$(runtime_dir)"
  for c in "$d/bin/python3" "$d/bin/python3.12" "$d/bin/python"; do
    if [ -x "$c" ]; then echo "$c"; return 0; fi
  done
  return 1
}

# 离线包里的运行时压缩包路径；$1=目标 id（兼容旧的 amd64/arm64 写法）
runtime_tarball() {
  local key="${1:-$(target_id)}" d
  case "$key" in
    amd64) key="linux-amd64" ;;
    arm64) key="linux-arm64" ;;
  esac
  d="$(target_runtime_dir "$key")"
  ls -1 "$d"/*.tar.gz 2>/dev/null | head -1
}

# 解释器优先级：$VENV_PY -> .venv -> .venv_path 记录 -> 随包运行时 -> 系统 python3
resolve_python() {
  local venv="${VENV_DIR:-}"
  if [ -n "${VENV_PY:-}" ] && [ -x "${VENV_PY}" ]; then echo "$VENV_PY"; return 0; fi
  [ -n "$venv" ] && [ -x "$venv/bin/python" ] && { echo "$venv/bin/python"; return 0; }
  [ -x "$PROJECT_ROOT/.venv/bin/python" ] && { echo "$PROJECT_ROOT/.venv/bin/python"; return 0; }
  if [ -f "$PROJECT_ROOT/.venv_path" ]; then
    local p; p="$(cat "$PROJECT_ROOT/.venv_path" 2>/dev/null || true)"
    [ -n "$p" ] && [ -x "$p/bin/python" ] && { echo "$p/bin/python"; return 0; }
  fi
  runtime_python && return 0
  command -v python3 || return 1
}

# 解释器版本号（如 3.12）
python_version_of() {
  "$1" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || echo "?"
}

# 模型缓存目录（与 backend/settings.py 保持一致）
model_home() {
  echo "${PADDLEX_OCR_MODEL_HOME:-$PROJECT_ROOT/models/paddlex_home}"
}

print_banner() {
  cat <<EOF
-------------------------------------------------------------
 PaddleX OCR Web · $1
 项目目录 : $PROJECT_ROOT
 CPU 架构 : $(uname -m) -> $(detect_arch)
 配置文件 : $CONFIG_FILE
 模型目录 : $(model_home)/official_models
 运行时   : $(if runtime_python >/dev/null 2>&1; then echo "随包 $(runtime_python)"; elif command -v python3 >/dev/null 2>&1; then echo "系统 $(command -v python3)"; else echo "未找到（将使用离线包内运行时）"; fi)
-------------------------------------------------------------
EOF
}
