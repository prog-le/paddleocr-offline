#!/usr/bin/env bash
# =============================================================================
# 服务状态与健康检查
# 用法: ./scripts/status.sh
#   exit 0 = 健康；1 = 异常
# =============================================================================
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

PORT="$(config_get server.port 8000)"
HOST="127.0.0.1"
PID_FILE="$PROJECT_ROOT/data/server.pid"

echo "---------------------------------------------"
if [ -f "$PID_FILE" ]; then
  PID="$(cat "$PID_FILE")"
  if kill -0 "$PID" 2>/dev/null; then
    echo "进程状态 : 运行中（PID ${PID}）"
  else
    echo "进程状态 : PID 文件存在但进程已退出"
  fi
else
  echo "进程状态 : 无 PID 文件（可能是前台/systemd 运行）"
fi
echo "监听地址 : http://$HOST:$PORT"
echo "日志目录 : $PROJECT_ROOT/logs"

if ! command -v curl >/dev/null 2>&1; then
  warn "未安装 curl，跳过 HTTP 健康检查"
  exit 0
fi

BODY="$(curl -s --max-time 10 "http://$HOST:$PORT/api/health" || true)"
if [ -z "$BODY" ]; then
  echo "健康检查 : 无法连接（服务未启动或端口不同）"
  exit 1
fi
echo "健康检查 : $BODY"
PYCODE="$(printf '%s' "$BODY" | tr -d '\n')"
case "$PYCODE" in
  *'"status":"ok"'*) echo "结论     : 正常"; exit 0 ;;
  *'"status": "ok"'*) echo "结论     : 正常"; exit 0 ;;
  *) echo "结论     : 降级（请查看上面的 issues 字段）"; exit 1 ;;
esac
