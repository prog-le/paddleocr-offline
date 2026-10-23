#!/usr/bin/env bash
# =============================================================================
# 离线服务器：导入镜像 + 配置 + 启动 + 健康检查
#
# 用法：
#   ./docker/import_and_start.sh --tar /opt/paddlex-ocr-web-1.0.0-amd64.image.tar.gz
#   ./docker/import_and_start.sh --tar ... --port 8080 --admin-password 'YourPass'
#   ./docker/import_and_start.sh --tar ... --no-start     # 只导入不启动
#   ./docker/import_and_start.sh --tar gov.image.tar.gz --gpu   # GPU 版：叠加载 GPU 覆盖层
#
# 全程不联网：docker load + docker compose up（镜像内已含全部依赖与模型）
# =============================================================================
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/../scripts/env.sh"

TAR_PATH=""
GPU=0
PORT=""
ADMIN_PASSWORD=""
NO_START=0
COMPOSE_DIR="$PROJECT_ROOT/docker"

while [ $# -gt 0 ]; do
  case "$1" in
    --gpu) GPU=1; shift ;;
    --tar) TAR_PATH="$2"; shift 2 ;;
    --tar=*) TAR_PATH="${1#*=}"; shift ;;
    --port) PORT="$2"; shift 2 ;;
    --port=*) PORT="${1#*=}"; shift ;;
    --admin-password) ADMIN_PASSWORD="$2"; shift 2 ;;
    --no-start) NO_START=1; shift ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) die "未知参数：$1" ;;
  esac
done

require_cmd docker "请先安装 Docker 20.10+"
docker info >/dev/null 2>&1 || die "Docker daemon 未运行或当前用户无权限（可尝试 sudo）"

[ -n "$TAR_PATH" ] || die "请用 --tar 指定镜像压缩包"
[ -f "$TAR_PATH" ] || die "镜像包不存在：$TAR_PATH"

print_banner "离线导入并启动"

step "1/5 校验镜像包"
if [ -f "${TAR_PATH}.sha256" ]; then
  if command -v sha256sum >/dev/null 2>&1; then
    ( cd "$(dirname "$TAR_PATH")" && sha256sum -c "$(basename "$TAR_PATH").sha256" ) || die "校验失败，镜像包可能损坏"
  elif command -v shasum >/dev/null 2>&1; then
    EXPECT="$(awk '{print $1}' "${TAR_PATH}.sha256")"
    ACTUAL="$(shasum -a 256 "$TAR_PATH" | awk '{print $1}')"
    [ "$EXPECT" = "$ACTUAL" ] || die "校验失败：期望 $EXPECT 实际 $ACTUAL"
  fi
  ok "sha256 校验通过"
else
  warn "未找到 .sha256 文件，跳过校验"
fi

step "2/5 导入镜像（docker load）"
LOAD_OUT="$(docker load -i "$TAR_PATH")"
echo "$LOAD_OUT"
# 从输出里解析出镜像 tag，例如：Loaded image: paddlex-ocr-web:1.0.0-amd64
IMG="$(printf '%s\n' "$LOAD_OUT" | sed -n 's/^Loaded image: //p' | tail -1)"
[ -n "$IMG" ] || IMG="$(printf '%s\n' "$LOAD_OUT" | sed -n 's/^Loaded image ID: //p' | tail -1)"
[ -n "$IMG" ] || die "无法从 docker load 输出中解析镜像名，请手动执行 docker images 确认"
ok "镜像已导入：$IMG"

# 校验镜像架构与当前主机一致
IMG_ARCH="$(docker image inspect "$IMG" --format '{{.Architecture}}' 2>/dev/null || echo unknown)"
HOST_ARCH="$(detect_arch)"
if [ "$IMG_ARCH" != "unknown" ]; then
  case "$HOST_ARCH:$IMG_ARCH" in
    amd64:amd64|arm64:arm64) ok "架构匹配：$IMG_ARCH" ;;
    *) warn "镜像架构 $IMG_ARCH 与主机 $HOST_ARCH 不一致，容器可能无法启动！请使用对应架构的 tar 包" ;;
  esac
fi

step "3/5 准备 compose 环境"
cd "$COMPOSE_DIR"
[ -f .env ] || cp .env.example .env
mkdir -p "$PROJECT_ROOT/data" "$PROJECT_ROOT/logs"
# 容器内以 uid 10001 运行，挂载目录需可写
chmod 0777 "$PROJECT_ROOT/data" "$PROJECT_ROOT/logs" 2>/dev/null || \
  warn "无法修改 data/logs 权限，若容器报权限错误请执行：sudo chown -R 10001:10001 data logs"

# 更新/写入 .env 关键项
set_env() {
  local key="$1" value="$2"
  if grep -q "^${key}=" .env 2>/dev/null; then
    # 用 python 做安全替换，避免 sed 转义问题
    python3 - "$key" "$value" <<'PY'
import sys, pathlib
key, value = sys.argv[1], sys.argv[2]
p = pathlib.Path(".env")
lines = p.read_text(encoding="utf-8").splitlines()
out = [f"{key}={value}" if ln.startswith(key + "=") else ln for ln in lines]
p.write_text("\n".join(out) + "\n", encoding="utf-8")
PY
  else
    printf '%s=%s\n' "$key" "$value" >> .env
  fi
}

set_env IMAGE "$IMG"
[ -n "$PORT" ] && set_env HOST_PORT "$PORT"
[ -n "$ADMIN_PASSWORD" ] && set_env ADMIN_PASSWORD "$ADMIN_PASSWORD"
ok "compose 配置已更新：$(pwd)/.env"
info "  IMAGE=$(grep '^IMAGE=' .env | cut -d= -f2-)"
info "  HOST_PORT=$(grep '^HOST_PORT=' .env | cut -d= -f2-)"

if [ "$NO_START" -eq 1 ]; then
  ok "已按 --no-start 跳过启动。手动启动：cd $COMPOSE_DIR && docker compose up -d"
  exit 0
fi

step "4/5 启动容器"
COMPOSE_FILES=(-f docker-compose.yml)
if [ "$GPU" -eq 1 ]; then
  COMPOSE_FILES+=(-f docker-compose.gpu.yml)
  if command -v nvidia-smi >/dev/null 2>&1; then
    info "检测到 NVIDIA 驱动：$(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)"
  else
    warn "本机没有 nvidia-smi：GPU 版容器可能无法启动（需要驱动 + nvidia-container-toolkit）"
  fi
fi
if docker compose version >/dev/null 2>&1; then
  docker compose "${COMPOSE_FILES[@]}" up -d --no-build
elif command -v docker-compose >/dev/null 2>&1; then
  docker-compose "${COMPOSE_FILES[@]}" up -d --no-build
else
  die "未找到 docker compose 插件，请安装 docker-compose-plugin"
fi
ok "容器已启动"

step "5/5 等待健康检查"
PORT_FINAL="$(grep '^HOST_PORT=' .env | cut -d= -f2- || echo 8000)"
PORT_FINAL="${PORT_FINAL:-8000}"
HEALTHY=0
for i in $(seq 1 40); do
  if command -v curl >/dev/null 2>&1; then
    if curl -fsS --max-time 3 "http://127.0.0.1:${PORT_FINAL}/api/health" >/dev/null 2>&1; then
      HEALTHY=1; break
    fi
  else
    if docker exec "${CONTAINER_NAME:-paddlex-ocr-web}" python /app/scripts/docker_health.py >/dev/null 2>&1; then
      HEALTHY=1; break
    fi
  fi
  sleep 3
  printf '.'
done
echo

if [ "$HEALTHY" -eq 1 ]; then
  ok "服务已就绪"
else
  warn "健康检查未通过（首次启动模型预热较慢，可继续等待 1~2 分钟）"
  warn "排查：docker logs -f ${CONTAINER_NAME:-paddlex-ocr-web}"
fi

cat <<EOF

-------------------------------------------------------------
 访问地址     : http://<服务器IP>:${PORT_FINAL}
 管理后台     : http://<服务器IP>:${PORT_FINAL}/admin
 容器名       : ${CONTAINER_NAME:-paddlex-ocr-web}
 查看日志     : docker logs -f ${CONTAINER_NAME:-paddlex-ocr-web}
 进入容器     : docker exec -it ${CONTAINER_NAME:-paddlex-ocr-web} bash
 停止服务     : cd ${COMPOSE_DIR} && docker compose down
 数据目录     : ${PROJECT_ROOT}/data
 日志目录     : ${PROJECT_ROOT}/logs
-------------------------------------------------------------
EOF
