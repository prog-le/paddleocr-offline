#!/usr/bin/env bash
# =============================================================================
# 依赖离线包预下载（在【有外网】的机器上执行）
#
# 平台矩阵（与 Paddle/PaddleX 上游实际发布的轮子一致）：
#   * linux-amd64 / linux-arm64  Paddle 3.4.0 CPU（PyPI 只有 x86_64，
#     因此 Linux 追加 Paddle 官方 CPU 源，该源同时提供 x86_64/aarch64）；
#   * linux-amd64-gpu            Paddle 3.4.0 GPU（官方 GPU 轮子仅 x86_64）；
#   * macos-arm64                Paddle 3.3.1 CPU（PyPI，仅 Apple Silicon）；
#   * windows-amd64              Paddle 3.3.1 CPU（PyPI）；
#   * macOS Intel / Windows ARM64 / 非 x86_64 GPU 上游没有轮子，脚本会明确拒绝。
#
# 用法：
#   ./scripts/download_wheels.sh                       # 当前平台默认架构（Linux=amd64+arm64）
#   ./scripts/download_wheels.sh --os linux --arch arm64
#   ./scripts/download_wheels.sh --os macos --arch arm64
#   ./scripts/download_wheels.sh --os windows --arch amd64
#   ./scripts/download_wheels.sh --os linux --arch amd64 --device gpu --cuda cu129
#   ./scripts/download_wheels.sh --native              # 只下当前机器平台/架构
#
# 依赖说明：
#   * wheel 的 cp 标签必须与随包 CPython 3.12 一致（--python 3.12）；
#   * macOS/Windows 使用各平台的 requirements-*.txt（Paddle 版本低于 Linux）；
#   * 交叉下载时 pip 对 platform_system 标记的判断可能与目标机不同，
#     脚本会在结束时做关键包存在性校验，缺包会直接报错而不是打出一个坏包。
# =============================================================================
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

PY_VERSION="3.12"
ARCHES=()
NATIVE=0
TARGET_OS="linux"
OUT_ROOT="$PROJECT_ROOT/offline/wheels"
OUT_EXPLICIT=0
DEVICE="cpu"           # cpu | gpu
CUDA="cu129"           # gpu 时的 CUDA 版本（cu126/cu129/cu130）
PADDLE_INDEX="https://www.paddlepaddle.org.cn/packages/stable/cpu/"
PYPI_INDEX="${PYPI_INDEX:-https://pypi.org/simple}"

while [ $# -gt 0 ]; do
  case "$1" in
    --os) TARGET_OS="$2"; shift 2 ;;
    --os=*) TARGET_OS="${1#*=}"; shift ;;
    --arch) ARCHES+=("$2"); shift 2 ;;
    --arch=*) ARCHES+=("${1#*=}"); shift ;;
    --python) PY_VERSION="$2"; shift 2 ;;
    --python=*) PY_VERSION="${1#*=}"; shift ;;
    --native) NATIVE=1; shift ;;
    --device) DEVICE="$(echo "$2" | tr '[:upper:]' '[:lower:]')"; shift 2 ;;
    --device=*) DEVICE="$(echo "${1#*=}" | tr '[:upper:]' '[:lower:]')"; shift ;;
    --cuda) CUDA="$2"; shift 2 ;;
    --cuda=*) CUDA="${1#*=}"; shift ;;
    --out) OUT_ROOT="$2"; OUT_EXPLICIT=1; shift 2 ;;
    --out=*) OUT_ROOT="${1#*=}"; OUT_EXPLICIT=1; shift ;;
    -h|--help) sed -n '2,28p' "$0"; exit 0 ;;
    *) die "未知参数：$1" ;;
  esac
done

case "$TARGET_OS" in
  linux)   [ ${#ARCHES[@]} -gt 0 ] || ARCHES=(amd64 arm64) ;;
  macos)   [ ${#ARCHES[@]} -gt 0 ] || ARCHES=(arm64) ;;
  windows) [ ${#ARCHES[@]} -gt 0 ] || ARCHES=(amd64) ;;
  *) die "不支持的 --os：$TARGET_OS（可选 linux / macos / windows）" ;;
esac

case "$DEVICE" in
  cpu) ;;
  gpu)
    [ "$TARGET_OS" = "linux" ] || die "$(target_support_note "${TARGET_OS}-$(echo "${ARCHES[0]}")-gpu")"
    PADDLE_INDEX="https://www.paddlepaddle.org.cn/packages/stable/${CUDA}/"
    for a in "${ARCHES[@]}"; do
      [ "$a" = "arm64" ] && die "paddlepaddle-gpu 没有 linux_aarch64 轮子（官方只发 amd64）。
     ARM 服务器要用 NVIDIA GPU，请改用 NVIDIA NGC 的 paddle 镜像；
     或只下载 CPU 版：./scripts/download_wheels.sh --os linux --arch arm64 --device cpu"
    done
    ;;
  *) die "--device 只能是 cpu 或 gpu" ;;
esac

if [ "$DEVICE" = "gpu" ]; then
  case "$CUDA" in
    cu126|cu129|cu130) ;;
    *) die "--cuda 目前支持 cu126 / cu129 / cu130（paddle 3.4.0 在 cu129/cu130 有轮子）" ;;
  esac
  # GPU 轮子默认放到独立目录，避免和 CPU 包混在一起
  [ "$OUT_EXPLICIT" -eq 1 ] || OUT_ROOT="$PROJECT_ROOT/offline/wheels-gpu"
fi

if [ "$NATIVE" -eq 1 ]; then
  [ "$TARGET_OS" = "$(detect_os)" ] || die "--native 要求构建机与目标系统一致（当前 $(detect_os)，目标 ${TARGET_OS}）"
  ARCHES=("$(detect_arch)")
fi

print_banner "依赖离线包下载"

PY="$(command -v python3 || true)"
[ -n "$PY" ] || die "需要 python3 来执行 pip 下载"
PY_VER="$(echo "$PY_VERSION" | tr -d '.')"      # 3.10 -> 310
HOST_OS="$(detect_os)"
HOST_ARCH="$(detect_arch)"

for ARCH in "${ARCHES[@]}"; do
  TARGET="${TARGET_OS}-${ARCH}"
  [ "$DEVICE" = "gpu" ] && TARGET="${TARGET}-gpu"
  target_supported "$TARGET" || die "$(target_support_note "$TARGET")"

  REQ_NAME="$(target_requirements "$TARGET")" || die "没有 ${TARGET} 对应的 requirements 文件"
  REQ_FILE="$PROJECT_ROOT/$REQ_NAME"
  [ -f "$REQ_FILE" ] || die "缺少 $REQ_NAME"

  # 目录约定：Linux 沿用 <arch>，macOS/Windows 用 <target>，互不混装
  if [ "$TARGET_OS" = "linux" ]; then
    DEST="$OUT_ROOT/$ARCH"
  else
    DEST="$OUT_ROOT/$TARGET"
  fi
  mkdir -p "$DEST"

  # 平台标签：pip 只接受「列出来的」标签，不会自动向下兼容
  PLATFORM_ARGS=()
  case "$TARGET_OS" in
    linux)
      TAG="$(wheel_arch "$ARCH")"
      for _n in $(seq 5 35); do
        PLATFORM_ARGS+=(--platform "manylinux_2_${_n}_${TAG}")
      done
      PLATFORM_ARGS+=(
        --platform "manylinux2014_${TAG}"
        --platform "manylinux2010_${TAG}"
        --platform "manylinux1_${TAG}"
        --platform "linux_${TAG}"
      )
      ;;
    macos)
      # Apple Silicon 轮子通常标 macosx_11_0_arm64 或 universal2
      for _n in 10 11 12 13 14 15; do
        PLATFORM_ARGS+=(--platform "macosx_${_n}_0_arm64")
        PLATFORM_ARGS+=(--platform "macosx_${_n}_0_universal2")
      done
      PLATFORM_ARGS+=(--platform "macosx_10_9_universal2")
      ;;
    windows)
      PLATFORM_ARGS+=(--platform "win_amd64")
      ;;
  esac

  # GPU：requirements 里的 CPU 版 paddlepaddle 必须剔除
  EXTRA_PKGS=()
  GPU_REQ_TMP=""
  if [ "$DEVICE" = "gpu" ]; then
    GPU_REQ_TMP="$(mktemp "${TMPDIR:-/tmp}/pdx-req-gpu.XXXXXX")"
    REQ_FILE="$GPU_REQ_TMP"
    grep -v -E '^[[:space:]]*paddlepaddle([<>=!~ ]|$)' "$PROJECT_ROOT/requirements.txt" > "$REQ_FILE"
    EXTRA_PKGS=("paddlepaddle-gpu==3.4.0")
    if [ -n "$(ls -1 "$DEST"/paddlepaddle-[0-9]*.whl 2>/dev/null || true)" ]; then
      warn "$DEST 里有 CPU 版 paddlepaddle 轮子，将被删除（CPU/GPU 轮子不能混放）"
    fi
    rm -f "$DEST"/paddlepaddle-[0-9]*.whl
    rm -f "$DEST"/.requirements-*.txt
  else
    rm -f "$DEST"/paddlepaddle_gpu-*.whl
  fi

  step "下载 ${TARGET}（目标 Python ${PY_VERSION}，${DEVICE}，依赖 ${REQ_NAME}）"

  INDEX_ARGS=(--index-url "$PYPI_INDEX")
  [ "$TARGET_OS" = "linux" ] && INDEX_ARGS+=(--extra-index-url "$PADDLE_INDEX")

  # 只有构建机与目标平台完全一致时才走原生解析
  if [ "$TARGET_OS" = "$HOST_OS" ] && [ "$ARCH" = "$HOST_ARCH" ]; then
    info "构建机与目标一致，直接解析依赖"
    "$PY" -m pip download \
      --only-binary=:all: \
      --python-version "$PY_VERSION" \
      "${INDEX_ARGS[@]}" \
      -d "$DEST" \
      -r "$REQ_FILE" "${EXTRA_PKGS[@]}" \
      || die "依赖下载失败（${TARGET}）。若个别包无 wheel，请在该平台机器上执行本脚本"
  else
    info "交叉下载（${TARGET}，Python ${PY_VERSION}）"
    "$PY" -m pip download \
      --only-binary=:all: \
      "${PLATFORM_ARGS[@]}" \
      --python-version "$PY_VERSION" \
      --implementation cp \
      --abi "cp$PY_VER" \
      "${INDEX_ARGS[@]}" \
      -d "$DEST" \
      -r "$REQ_FILE" "${EXTRA_PKGS[@]}" \
      || die "交叉下载失败（${TARGET}）。某些包可能只提供源码包，请改在 ${TARGET_OS}/${ARCH} 机器上执行：./scripts/download_wheels.sh --os ${TARGET_OS} --arch ${ARCH} --native"
  fi

  # GPU 专属：补下 Linux 标记的 CUDA 依赖
  # （pip 在非 Linux 构建机上会按当前解释器判断 platform_system 标记）
  if [ "$DEVICE" = "gpu" ]; then
    PLAT_ARGS=("${PLATFORM_ARGS[@]}" --implementation cp --abi "cp$PY_VER")
    if [ "$TARGET_OS" = "$HOST_OS" ] && [ "$ARCH" = "$HOST_ARCH" ]; then
      PLAT_ARGS=()
    fi
    ROUND=1
    while [ "$ROUND" -le 6 ]; do
      MISSING="$("$PY" "$PROJECT_ROOT/scripts/gpu_deps.py" --dir "$DEST" --target-python "$PY_VERSION" 2>/dev/null || true)"
      [ -n "$MISSING" ] || break
      MISSING_ARR=()
      while IFS= read -r _line; do
        [ -n "$_line" ] && MISSING_ARR+=("$_line")
      done <<< "$MISSING"
      info "补下 Linux 专属 CUDA 依赖（第 ${ROUND} 轮，${#MISSING_ARR[@]} 个）"
      "$PY" -m pip download --only-binary=:all: \
        "${PLAT_ARGS[@]}" --python-version "$PY_VERSION" \
        --index-url "$PYPI_INDEX" \
        -d "$DEST" "${MISSING_ARR[@]}" \
        || die "CUDA 依赖下载失败（第 ${ROUND} 轮）。
     可换镜像重试：PYPI_INDEX=https://mirrors.ustc.edu.cn/pypi/simple PIP_TRUSTED_HOST=mirrors.ustc.edu.cn"
      ROUND=$((ROUND + 1))
    done
    FINAL_MISSING="$("$PY" "$PROJECT_ROOT/scripts/gpu_deps.py" --dir "$DEST" --target-python "$PY_VERSION" 2>/dev/null || true)"
    [ -z "$FINAL_MISSING" ] || die "仍有 Linux 专属依赖未下载完整：
$FINAL_MISSING"
    ok "CUDA 依赖已齐全（nvidia-* / cuda-python）"
  fi

  # 关键包存在性校验：宁可在这里失败，也不要打出一个装不上的包
  case "$TARGET_OS" in
    linux)
      if [ "$DEVICE" = "gpu" ]; then
        ls "$DEST"/paddlepaddle_gpu-*.whl >/dev/null 2>&1 \
          || die "$TARGET 缺少 paddlepaddle-gpu wheel（检查 Paddle 官方 ${CUDA} 源是否可访问）"
        [ -n "$GPU_REQ_TMP" ] && rm -f "$GPU_REQ_TMP"
      else
        ls "$DEST"/paddlepaddle-[0-9]*.whl >/dev/null 2>&1 \
          || die "$TARGET 缺少 paddlepaddle wheel（检查 Paddle 官方源是否可访问）"
      fi
      ;;
    macos)
      ls "$DEST"/paddlepaddle-*macosx*arm64*.whl >/dev/null 2>&1 \
        || die "$TARGET 缺少 macOS arm64 的 paddlepaddle 3.3.1 wheel（PyPI 无该轮子或被镜像屏蔽）"
      ;;
    windows)
      ls "$DEST"/paddlepaddle-*win_amd64*.whl >/dev/null 2>&1 \
        || die "$TARGET 缺少 Windows amd64 的 paddlepaddle 3.3.1 wheel（PyPI 无该轮子或被镜像屏蔽）"
      ;;
  esac
  ls "$DEST"/paddlex-*.whl >/dev/null 2>&1 || die "$TARGET 缺少 paddlex wheel"

  # 记录运行时一致性提示
  RT_DIR="$(target_runtime_dir "${TARGET_OS}-${ARCH}")"
  if [ ! -f "$RT_DIR/runtime-info.txt" ]; then
    warn "还没有 ${TARGET} 的随包 CPython 运行时，可执行：./scripts/download_python_runtime.sh --os ${TARGET_OS} --arch ${ARCH}"
  fi

  ( cd "$DEST" && ls -1 *.whl | sort > wheel-list.txt )
  WHEEL_COUNT="$(wc -l < "$DEST/wheel-list.txt" | tr -d ' ')"
  SIZE="$(du -sh "$DEST" | awk '{print $1}')"
  ok "$TARGET 完成（${DEVICE}）：$WHEEL_COUNT 个 wheel，合计 $SIZE -> $DEST"
done

LAST_ARCH="${ARCHES[$((${#ARCHES[@]} - 1))]}"
LAST_TARGET="${TARGET_OS}-${LAST_ARCH}"
[ "$DEVICE" = "gpu" ] && LAST_TARGET="${LAST_TARGET}-gpu"

step "汇总"
for ARCH in "${ARCHES[@]}"; do
  T="${TARGET_OS}-${ARCH}$([ "$DEVICE" = "gpu" ] && echo "-gpu")"
  if [ "$TARGET_OS" = "linux" ]; then D="$OUT_ROOT/$ARCH"; else D="$OUT_ROOT/$T"; fi
  printf '  %-16s %s 个包  %s\n' "$T" "$(wc -l < "$D/wheel-list.txt" | tr -d ' ')" "$(du -sh "$D" | awk '{print $1}')"
done

if [ "$DEVICE" = "gpu" ]; then
  cat <<EOF

GPU 版已下载（${CUDA}）。运行机需要：
  * NVIDIA 驱动 >= 575.57.08（cu129），\`nvidia-smi\` 正常；
  * glibc >= 2.17（随包 CPython 运行时的一致要求）。

下一步：
  1) 校验轮子（无需显卡）：./scripts/verify_gpu_wheel.sh --dir $(target_wheels_dir "$LAST_TARGET")
  2) 下载随包 Python 运行时：./scripts/download_python_runtime.sh --os linux --arch ${LAST_ARCH}
  3) 下载模型（与设备无关）：python3 scripts/download_models.py
  4) 一键打包：./scripts/build_deploy_package.sh --targets ${LAST_TARGET}
  5) 目标机安装：./scripts/install_offline.sh --device gpu
EOF
  exit 0
fi

cat <<EOF

下一步：
  1) 下载随包 Python 运行时：./scripts/download_python_runtime.sh --os ${TARGET_OS} --arch ${LAST_ARCH}
  2) 下载并打包模型（需要外网）：python3 scripts/download_models.py --out models/paddlex_home
  3) 一键打包：./scripts/build_deploy_package.sh --targets ${LAST_TARGET}
  4) 目标机安装：
       Linux/macOS: ./scripts/install_offline.sh
       Windows:     powershell -ExecutionPolicy Bypass -File scripts\\install_windows.ps1
EOF
