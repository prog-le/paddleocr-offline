#!/usr/bin/env bash
# =============================================================================
# 导出镜像为离线 tar 包（在构建机上执行）
#
# 用法：
#   ./docker/export_image.sh                      # 导出当前架构镜像
#   ./docker/export_image.sh --arch amd64
#   ./docker/export_image.sh --arch all           # 两个架构都导出
#   ./docker/export_image.sh --arch arm64 --no-gzip
#   ./docker/export_image.sh --arch amd64 --gpu   # 导出 GPU 版镜像（tag 带 -gpu）
#
# 产物：dist/paddlex-ocr-web-<ver>-<arch>.image.tar.gz
# 离线服务器：docker load -i 该文件
# =============================================================================
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/../scripts/env.sh"

ARCH_TARGET="native"
GZIP=1
GPU=0
VERSION="$(cd "$PROJECT_ROOT" && python3 -c 'import re,pathlib;m=re.search(r"__version__\s*=\s*\"([^\"]+)\"", pathlib.Path("backend/__init__.py").read_text());print(m.group(1) if m else "1.0.0")')"

while [ $# -gt 0 ]; do
  case "$1" in
    --arch) ARCH_TARGET="$2"; shift 2 ;;
    --arch=*) ARCH_TARGET="${1#*=}"; shift ;;
    --version) VERSION="$2"; shift 2 ;;
    --no-gzip) GZIP=0; shift ;;
    --gpu) GPU=1; shift ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) die "未知参数：$1" ;;
  esac
done

require_cmd docker "请先安装 Docker"
[ "$ARCH_TARGET" = "native" ] && ARCH_TARGET="$(detect_arch)"
if [ "$ARCH_TARGET" = "all" ]; then TARGETS=(amd64 arm64); else TARGETS=("$ARCH_TARGET"); fi

DIST="$PROJECT_ROOT/dist"
mkdir -p "$DIST"

print_banner "导出离线镜像"

for ARCH in "${TARGETS[@]}"; do
  SUFFIX=""
  [ "$GPU" -eq 1 ] && SUFFIX="-gpu"
  TAG="paddlex-ocr-web:${VERSION}-${ARCH}${SUFFIX}"
  if ! docker image inspect "$TAG" >/dev/null 2>&1; then
    warn "本机不存在镜像 ${TAG}，跳过（请先 ./docker/build_image.sh --arch ${ARCH}$([ "$GPU" -eq 1 ] && echo " --device gpu")）"
    continue
  fi
  step "导出 $TAG"
  if [ "$GZIP" -eq 1 ]; then
    OUT="$DIST/paddlex-ocr-web-${VERSION}-${ARCH}${SUFFIX}.image.tar.gz"
    docker save "$TAG" | gzip -9 > "$OUT"
  else
    OUT="$DIST/paddlex-ocr-web-${VERSION}-${ARCH}${SUFFIX}.image.tar"
    docker save "$TAG" -o "$OUT"
  fi
  ok "${OUT}（$(du -h "$OUT" | awk '{print $1}')）"
  # 附带校验值，便于传输后核对
  if command -v sha256sum >/dev/null 2>&1; then
    ( cd "$DIST" && sha256sum "$(basename "$OUT")" > "$(basename "$OUT").sha256" )
  elif command -v shasum >/dev/null 2>&1; then
    ( cd "$DIST" && shasum -a 256 "$(basename "$OUT")" > "$(basename "$OUT").sha256" )
  fi
done

echo
ok "导出完成，目录：$DIST"
cat <<EOF

离线服务器操作：
  # 1. 传输（示例）
  scp $DIST/paddlex-ocr-web-${VERSION}-<arch>.image.tar.gz user@server:/opt/
  # 2. 导入并启动（脚本会自动 load + 配置 compose + 等待健康检查）
  ./docker/import_and_start.sh --tar /opt/paddlex-ocr-web-${VERSION}-<arch>.image.tar.gz
EOF
