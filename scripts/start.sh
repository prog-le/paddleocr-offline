#!/usr/bin/env bash
# =============================================================================
# 启动服务（本地部署）
#
# 用法：
#   ./scripts/start.sh                # 前台运行（Ctrl+C 停止，适合 systemd/docker）
#   ./scripts/start.sh --daemon       # 后台运行，PID 写入 data/server.pid
#   ./scripts/start.sh --port 8080    # 临时覆盖端口
#   ./scripts/start.sh --mock         # MOCK 模式（不加载模型，仅联调/自检）
# =============================================================================
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

DAEMON=0
PORT_OVERRIDE=""
HOST_OVERRIDE=""
MOCK=0

while [ $# -gt 0 ]; do
  case "$1" in
    --daemon|-d) DAEMON=1; shift ;;
    --port) PORT_OVERRIDE="$2"; shift 2 ;;
    --port=*) PORT_OVERRIDE="${1#*=}"; shift ;;
    --host) HOST_OVERRIDE="$2"; shift 2 ;;
    --mock) MOCK=1; shift ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) die "未知参数：$1" ;;
  esac
done

# 解释器选择顺序：$VENV_DIR -> 项目 .venv -> .venv_path 记录 -> 当前环境 python3
# （macOS/开发机上常用 conda 环境，这里允许直接用当前解释器，避免必须先建 venv）
if [ -z "${VENV_DIR:-}" ]; then
  if [ -x "$PROJECT_ROOT/.venv/bin/python" ]; then
    VENV_DIR="$PROJECT_ROOT/.venv"
  elif [ -f "$PROJECT_ROOT/.venv_path" ] && [ -x "$(cat "$PROJECT_ROOT/.venv_path")/bin/python" ]; then
    VENV_DIR="$(cat "$PROJECT_ROOT/.venv_path")"
  fi
fi
if [ -n "${VENV_DIR:-}" ] && [ -x "$VENV_DIR/bin/python" ]; then
  VENV_PY="$VENV_DIR/bin/python"
else
  VENV_PY="$(command -v python3 || true)"
  if [ -z "$VENV_PY" ]; then
    if runtime_python >/dev/null 2>&1; then
      die "检测到随包运行时（$(runtime_python)），但依赖尚未安装。
     请先执行：./scripts/install_offline.sh   （它会用随包运行时创建 .venv 并离线装依赖）"
    fi
    die "系统没有 python3，也没有随包运行时。
     请在联网机器执行 ./scripts/download_python_runtime.sh --arch $(detect_arch) 后重新打包，
     或在本机执行 ./scripts/install_dev.sh（需要联网）。"
  fi
  warn "未找到项目虚拟环境，使用当前 Python：$VENV_PY"
  warn "（生产部署建议用 scripts/install_offline.sh 建独立 venv，避免与系统包冲突）"
fi
export VENV_PY
info "Python: $VENV_PY ($(python_version_of "$VENV_PY"))"

HOST="${HOST_OVERRIDE:-$(config_get server.host 0.0.0.0)}"
PORT="${PORT_OVERRIDE:-$(config_get server.port 8000)}"
LOG_LEVEL="$(config_get logging.level INFO)"
MODELS="$(model_home)/official_models"

# ---------------------------------------------------------------- 环境变量
export PADDLE_PDX_CACHE_HOME="$(model_home)"
export PADDLE_PDX_MODEL_SOURCE="${PADDLE_PDX_MODEL_SOURCE:-BOS}"
export PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK="True"
export PADDLE_PDX_DISABLE_TELEMETRY="True"
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 MODELSCOPE_OFFLINE=1
export PYTHONUNBUFFERED=1
[ "$MOCK" -eq 1 ] && export PADDLEX_OCR_MOCK=1

mkdir -p "$PROJECT_ROOT/data" "$PROJECT_ROOT/logs"

print_banner "启动服务"
info "监听地址: $HOST:$PORT"
info "模型目录: $MODELS"
info "日志目录: $PROJECT_ROOT/logs"

if [ ! -d "$MODELS" ] || [ -z "$(ls -A "$MODELS" 2>/dev/null)" ]; then
  if [ "$MOCK" -ne 1 ]; then
    warn "模型目录为空！服务可以启动，但识别任务会失败。"
    warn "请在联网机器执行 scripts/download_models.py 后拷贝 models/ 目录过来。"
  fi
fi

cd "$PROJECT_ROOT"
CMD=("$VENV_PY" -m uvicorn backend.main:app
     --host "$HOST" --port "$PORT" --workers 1
     --log-level "$(echo "$LOG_LEVEL" | tr '[:upper:]' '[:lower:]')"
     --no-access-log --timeout-keep-alive 75)

if [ "$DAEMON" -eq 1 ]; then
  PID_FILE="$PROJECT_ROOT/data/server.pid"
  if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    die "服务已在运行（PID $(cat "$PID_FILE")），如需重启请先 ./scripts/stop.sh"
  fi
  info "以后台方式启动，输出重定向到 logs/server.log"
  nohup "${CMD[@]}" >> "$PROJECT_ROOT/logs/server.log" 2>&1 &
  echo $! > "$PID_FILE"
  sleep 2
  if kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    ok "已启动，PID $(cat "$PID_FILE")"
    info "访问: http://<服务器IP>:$PORT    后台: http://<服务器IP>:$PORT/admin"
    info "查看日志: tail -f logs/server.log"
  else
    rm -f "$PID_FILE"
    die "启动失败，请查看 logs/server.log"
  fi
else
  info "前台运行中，Ctrl+C 停止"
  exec "${CMD[@]}"
fi
