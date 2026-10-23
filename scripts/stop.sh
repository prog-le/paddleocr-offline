#!/usr/bin/env bash
# =============================================================================
# 停止后台服务（仅对 ./scripts/start.sh --daemon 启动的进程有效）
# 用法: ./scripts/stop.sh [--force]
# =============================================================================
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

PID_FILE="$PROJECT_ROOT/data/server.pid"
if [ ! -f "$PID_FILE" ]; then
  warn "未找到 PID 文件 ${PID_FILE}（服务可能不是以 --daemon 方式启动的）"
  info "如为前台/systemd 运行，请用 systemctl stop 或在对应终端 Ctrl+C"
  exit 0
fi

PID="$(cat "$PID_FILE")"
if ! kill -0 "$PID" 2>/dev/null; then
  warn "进程 $PID 已不存在，清理 PID 文件"
  rm -f "$PID_FILE"
  exit 0
fi

info "正在停止 PID $PID …"
kill -TERM "$PID" 2>/dev/null || true
for i in $(seq 1 20); do
  kill -0 "$PID" 2>/dev/null || break
  sleep 1
done
if kill -0 "$PID" 2>/dev/null; then
  if [ "$FORCE" -eq 1 ]; then
    warn "优雅退出超时，强制结束"
    kill -9 "$PID" 2>/dev/null || true
  else
    die "进程未在 20 秒内退出，可执行 ./scripts/stop.sh --force"
  fi
fi
rm -f "$PID_FILE"
ok "已停止"
