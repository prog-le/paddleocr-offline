#!/usr/bin/env bash
# =============================================================================
# 构建镜像（在联网的构建机上执行）
#
# 用法：
#   ./docker/build_image.sh                     # 构建当前架构
#   ./docker/build_image.sh --arch amd64
#   ./docker/build_image.sh --arch arm64
#   ./docker/build_image.sh --arch all          # 两种架构各构建一个镜像（推荐，离线最稳）
#   ./docker/build_image.sh --arch amd64 --device gpu   # NVIDIA GPU 版镜像（仅 amd64）
#   ./docker/build_image.sh --arch amd64 --device gpu \
#       --wheels-dir offline/wheels-gpu/amd64           # GPU 轮子与 CPU 分开存放时指定
#
# 说明：
#   * 镜像按架构分别打 tag：paddlex-ocr-web:<ver>-amd64 / -arm64
#     （多架构 manifest 无法用 docker save 导出，因此离线交付采用"每架构一 tar"）
#   * 交叉架构构建需要 buildx + QEMU（docker run --privileged tonistiigi/binfmt --install all）
#     更稳的做法是在各自架构的机器上分别执行本脚本 --arch native
# =============================================================================
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/../scripts/env.sh"

ARCH_TARGET="native"
DEVICE="cpu"           # cpu | gpu
WHEELS_DIR_OPT=""      # 自定义 wheel 目录（相对项目根目录）
VERSION="$(cd "$PROJECT_ROOT" && python3 -c 'import re,pathlib;m=re.search(r"__version__\s*=\s*\"([^\"]+)\"", pathlib.Path("backend/__init__.py").read_text());print(m.group(1) if m else "1.0.0")')"
PYTHON_VERSION="${PYTHON_VERSION:-3.12}"

while [ $# -gt 0 ]; do
  case "$1" in
    --arch) ARCH_TARGET="$2"; shift 2 ;;
    --arch=*) ARCH_TARGET="${1#*=}"; shift ;;
    --version) VERSION="$2"; shift 2 ;;
    --python) PYTHON_VERSION="$2"; shift 2 ;;
    --device) DEVICE="$(echo "$2" | tr '[:upper:]' '[:lower:]')"; shift 2 ;;
    --device=*) DEVICE="$(echo "${1#*=}" | tr '[:upper:]' '[:lower:]')"; shift ;;
    --wheels-dir) WHEELS_DIR_OPT="$2"; shift 2 ;;
    --wheels-dir=*) WHEELS_DIR_OPT="${1#*=}"; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) die "未知参数：$1" ;;
  esac
done

require_cmd docker "请先安装 Docker 20.10+"
docker info >/dev/null 2>&1 || die "Docker daemon 未运行或当前用户无权限"

if [ "$ARCH_TARGET" = "native" ]; then
  ARCH_TARGET="$(detect_arch)"
fi
case "$DEVICE" in
  cpu|gpu) ;;
  *) die "--device 只能是 cpu 或 gpu" ;;
esac

if [ "$ARCH_TARGET" = "all" ]; then
  TARGETS=(amd64 arm64)
else
  TARGETS=("$ARCH_TARGET")
fi

if [ "$DEVICE" = "gpu" ]; then
  for a in "${TARGETS[@]}"; do
    [ "$a" != "amd64" ] && die "GPU 镜像仅支持 amd64（paddlepaddle-gpu 没有 arm64 轮子）"
  done
  DOCKERFILE="$PROJECT_ROOT/docker/Dockerfile.gpu"
  IMAGE_SUFFIX="-gpu"
else
  DOCKERFILE="$PROJECT_ROOT/docker/Dockerfile"
  IMAGE_SUFFIX=""
fi

MODELS="$(model_home)/official_models"
[ -d "$MODELS" ] && [ -n "$(ls -A "$MODELS" 2>/dev/null)" ] || \
  die "模型目录为空：${MODELS}。请先执行 python3 scripts/download_models.py（模型会被打进镜像）"

print_banner "构建 Docker 镜像"
info "版本: $VERSION  Python: $PYTHON_VERSION  设备: $DEVICE  目标架构: ${TARGETS[*]}"

# buildx 可用性
if docker buildx version >/dev/null 2>&1; then
  BUILDER_OK=1
else
  BUILDER_OK=0
  warn "未启用 docker buildx，跨架构构建将不可用（仅能构建当前架构）"
fi

for ARCH in "${TARGETS[@]}"; do
  case "$ARCH" in
    amd64) PLATFORM="linux/amd64" ;;
    arm64) PLATFORM="linux/arm64" ;;
    *) die "不支持的架构：$ARCH" ;;
  esac
  if [ -n "$WHEELS_DIR_OPT" ]; then
    WHEEL_DIR="$WHEELS_DIR_OPT"
    case "$WHEEL_DIR" in /*) ;; *) WHEEL_DIR="$PROJECT_ROOT/$WHEEL_DIR" ;; esac
  elif [ "$DEVICE" = "gpu" ] && [ -d "$PROJECT_ROOT/offline/wheels-gpu/$ARCH" ]; then
    # GPU 轮子默认放在独立目录：优先用它，避免误用 CPU 轮子
    WHEEL_DIR="$PROJECT_ROOT/offline/wheels-gpu/$ARCH"
  else
    WHEEL_DIR="$PROJECT_ROOT/offline/wheels/$ARCH"
  fi

  # 传给 Dockerfile 的 COPY 路径必须是相对构建上下文（项目根目录）
  case "$WHEEL_DIR" in
    "$PROJECT_ROOT"/*) WHEELS_REL="${WHEEL_DIR#$PROJECT_ROOT/}" ;;
    *) die "wheel 目录必须在项目目录内（当前：$WHEEL_DIR）" ;;
  esac
  [ -d "$WHEEL_DIR" ] && [ -n "$(ls -A "$WHEEL_DIR" 2>/dev/null)" ] || \
    die "缺少 ${WHEEL_DIR}。请先执行 ./scripts/download_wheels.sh --arch $ARCH --device $DEVICE"
  if [ "$DEVICE" = "gpu" ]; then
    ls "$WHEEL_DIR"/paddlepaddle_gpu-*.whl >/dev/null 2>&1 || \
      die "${WHEEL_DIR} 中没有 paddlepaddle-gpu 轮子。请执行：
     ./scripts/download_wheels.sh --arch amd64 --device gpu --cuda cu129 --out offline/wheels-gpu"
    # CUDA 运行库依赖（nvidia-* / cuda-python）必须齐全，否则镜像里 pip 会失败
    MISSING_DEPS="$(python3 "$PROJECT_ROOT/scripts/gpu_deps.py" --dir "$WHEEL_DIR" 2>/dev/null || true)"
    [ -z "$MISSING_DEPS" ] || die "${WHEEL_DIR} 缺少 CUDA 运行库依赖：
$(printf '%s\n' "$MISSING_DEPS" | sed 's/^/     - /')
     请执行：./scripts/download_wheels.sh --arch amd64 --device gpu --cuda cu129 --out offline/wheels-gpu"
  fi

  TAG="paddlex-ocr-web:${VERSION}-${ARCH}${IMAGE_SUFFIX}"
  step "构建 $ARCH -> ${TAG}（平台 ${PLATFORM}，wheel 目录 ${WHEELS_REL}）"

  if [ "$ARCH" = "$(detect_arch)" ]; then
    # 同架构：普通构建即可，最省事
    DOCKER_BUILDKIT=1 docker build \
      -f "$DOCKERFILE" \
      --build-arg "TARGETARCH=$ARCH" \
      --build-arg "APP_VERSION=$VERSION" \
      --build-arg "PYTHON_VERSION=$PYTHON_VERSION" \
      --build-arg "WHEELS_DIR=$WHEELS_REL" \
      -t "$TAG" \
      "$PROJECT_ROOT" || die "构建失败：$ARCH"
  else
    [ "$BUILDER_OK" -eq 1 ] || die "跨架构构建需要 buildx"
    warn "跨架构构建需要 QEMU 支持；若报 exec format error，请先执行："
    warn "  docker run --privileged --rm tonistiigi/binfmt --install all"
    docker buildx build \
      --platform "$PLATFORM" \
      -f "$DOCKERFILE" \
      --build-arg "TARGETARCH=$ARCH" \
      --build-arg "APP_VERSION=$VERSION" \
      --build-arg "PYTHON_VERSION=$PYTHON_VERSION" \
      --build-arg "WHEELS_DIR=$WHEELS_REL" \
      -t "$TAG" \
      --load \
      "$PROJECT_ROOT" || die "构建失败：${ARCH}（可改用同架构机器执行 --arch native）"
  fi

  SIZE="$(docker image inspect "$TAG" --format '{{.Size}}' | awk '{printf "%.1f GB", $1/1024/1024/1024}')"
  ok "$TAG 构建完成（${SIZE}）"
done

# 当前架构的镜像额外打一个不带架构后缀的 tag，方便 compose 默认值直接使用
CUR="$(detect_arch)"
if printf '%s\n' "${TARGETS[@]}" | grep -qx "$CUR"; then
  docker tag "paddlex-ocr-web:${VERSION}-${CUR}${IMAGE_SUFFIX}" "paddlex-ocr-web:${VERSION}${IMAGE_SUFFIX}"
  ok "已打别名 paddlex-ocr-web:${VERSION}${IMAGE_SUFFIX}"
fi

echo
ok "全部完成。下一步："
echo "  ./docker/export_image.sh --arch ${TARGETS[*]:-$(detect_arch)}    # 导出离线 tar 包"
