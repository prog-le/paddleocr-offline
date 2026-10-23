#!/usr/bin/env bash
# =============================================================================
# 依赖离线包预下载（在【有外网】的机器上执行）
#
# 双架构策略：
#   * 在 x86_64 机器上下载 amd64 与 arm64 两套 wheel；
#   * paddlepaddle 3.4.0 在 PyPI 只有 x86_64 版本，因此追加 Paddle 官方 CPU 源，
#     该源同时提供 linux_x86_64 与 linux_aarch64 两种 wheel；
#   * 纯 Python 包（py3-none-any）两套目录各存一份，不互相依赖。
#
# 用法：
#   ./scripts/download_wheels.sh                 # 同时下载 amd64 + arm64
#   ./scripts/download_wheels.sh --arch arm64    # 只下载 arm64
#   ./scripts/download_wheels.sh --native        # 只下载当前机器架构（最快，无需交叉解析）
#   ./scripts/download_wheels.sh --python 3.12   # 指定目标 Python 版本（默认 3.12，与随包运行时一致）
#   ./scripts/download_wheels.sh --arch amd64 --device gpu --cuda cu129
#                                                # NVIDIA GPU 版（paddlepaddle-gpu）
#
# 设备说明：
#   * --device cpu（默认）：paddlepaddle（CPU 版），amd64/arm64 都可用；
#   * --device gpu：paddlepaddle-gpu，**仅 amd64**（官方 GPU 轮子没有 linux_aarch64；
#     ARM 服务器如 Grace/Jetson 请用 NVIDIA NGC 的 paddle 镜像，本项目不提供该轮子）；
#   * --cuda 选择 CUDA 版本（cu126 / cu129 / cu130；3.4.0 目前发布在 cu129 / cu130）。
#
# 注意：wheel 的 cp 标签必须与目标机的运行时一致。本项目随包运行时是 CPython 3.12，
#       因此默认 --python 3.12；若改用其它版本，必须先换运行时再重新下载 wheel。
# =============================================================================
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

PY_VERSION="3.12"
ARCHES=()
NATIVE=0
OUT_ROOT="$PROJECT_ROOT/offline/wheels"
PADDLE_VERSION="3.4.0"
DEVICE="cpu"           # cpu | gpu
CUDA="cu129"           # gpu 时的 CUDA 版本（cu126/cu129/cu130）
PADDLE_INDEX="https://www.paddlepaddle.org.cn/packages/stable/cpu/"
PYPI_INDEX="${PYPI_INDEX:-https://pypi.org/simple}"

while [ $# -gt 0 ]; do
  case "$1" in
    --arch) ARCHES+=("$2"); shift 2 ;;
    --arch=*) ARCHES+=("${1#*=}"); shift ;;
    --python) PY_VERSION="$2"; shift 2 ;;
    --python=*) PY_VERSION="${1#*=}"; shift ;;
    --native) NATIVE=1; shift ;;
    --device) DEVICE="$(echo "$2" | tr '[:upper:]' '[:lower:]')"; shift 2 ;;
    --device=*) DEVICE="$(echo "${1#*=}" | tr '[:upper:]' '[:lower:]')"; shift ;;
    --cuda) CUDA="$2"; shift 2 ;;
    --cuda=*) CUDA="${1#*=}"; shift ;;
    --out) OUT_ROOT="$2"; shift 2 ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *) die "未知参数：$1" ;;
  esac
done

[ ${#ARCHES[@]} -gt 0 ] || ARCHES=(amd64 arm64)
case "$DEVICE" in
  cpu) ;;
  gpu)
    PADDLE_INDEX="https://www.paddlepaddle.org.cn/packages/stable/${CUDA}/"
    for a in "${ARCHES[@]}"; do
      [ "$a" = "arm64" ] && die "paddlepaddle-gpu 没有 linux_aarch64 轮子（官方只发 amd64）。
     ARM 服务器要用 NVIDIA GPU，请改用 NVIDIA NGC 的 paddle 镜像；
     或只下载 CPU 版：./scripts/download_wheels.sh --arch arm64 --device cpu"
    done
    ;;
  *) die "--device 只能是 cpu 或 gpu" ;;
esac
if [ "$DEVICE" = "gpu" ]; then
  case "$CUDA" in
    cu126|cu129|cu130) ;;
    *) die "--cuda 目前支持 cu126 / cu129 / cu130（paddle ${PADDLE_VERSION} 在 cu129/cu130 有轮子）" ;;
  esac
fi
if [ "$NATIVE" -eq 1 ]; then
  if [ "$(uname -s)" != "Linux" ]; then
    warn "--native 是给 Linux 目标机用的：当前系统 $(uname -s)，"
    warn "本机下载的 macOS 轮子无法用于 Linux 服务器。"
    warn "要给别的架构准备包，请显式指定：--arch amd64 / --arch arm64（交叉下载 Linux 轮子）"
    die "已中止"
  fi
  ARCHES=("$(detect_arch)")
fi

print_banner "依赖离线包下载"

# 目标机无 python3 时，运行时由 download_python_runtime.sh 提供；这里只做一致性提醒
if [ ! -f "$PROJECT_ROOT/offline/python/$(detect_arch)/runtime-info.txt" ]; then
  warn "还没有随包 Python 运行时：可执行 ./scripts/download_python_runtime.sh --arch $(detect_arch)"
  warn "（目标机已有 python3 且版本与 $PY_VERSION 一致时可忽略）"
fi

PY="$(command -v python3 || true)"
[ -n "$PY" ] || die "需要 python3 来执行 pip 下载"
PY_VER="$(echo "$PY_VERSION" | tr -d '.')"      # 3.10 -> 310
mkdir -p "$OUT_ROOT"

for ARCH in "${ARCHES[@]}"; do
  case "$ARCH" in
    amd64) TAG="x86_64" ;;
    arm64) TAG="aarch64" ;;
    *) die "不支持的架构：${ARCH}（可选 amd64 / arm64）" ;;
  esac
  DEST="$OUT_ROOT/$ARCH"
  mkdir -p "$DEST"

  # 平台标签：pip 只接受「列出来的」标签，不会自动向下兼容。
  # NVIDIA 新轮子用 manylinux_2_25 / 2_26 等中间版本，必须把所有小版本都列上，
  # 否则会误报 "No matching distribution found"（实测 nvidia-cuda-cupti 12.9.19）。
  PLATFORM_ARGS=()
  for _n in $(seq 5 35); do
    PLATFORM_ARGS+=(--platform "manylinux_2_${_n}_${TAG}")
  done
  PLATFORM_ARGS+=(
    --platform "manylinux2014_${TAG}"
    --platform "manylinux2010_${TAG}"
    --platform "manylinux1_${TAG}"
    --platform "linux_${TAG}"
  )
  step "下载 ${ARCH}（wheel 标签 linux_${TAG}，目标 Python ${PY_VERSION}，设备 ${DEVICE}$([ "$DEVICE" = gpu ] && echo " / ${CUDA}")）"

  # GPU 版：requirements 里 paddlepaddle（CPU）那行必须剔除，否则会同时拉 CPU/GPU 两份
  REQ_FILE="$PROJECT_ROOT/requirements.txt"
  EXTRA_PKGS=()
  GPU_REQ_TMP=""
  if [ "$DEVICE" = "gpu" ]; then
    # 临时文件放 /tmp：不要把中间产物写进 wheel 目录（那里只应有 *.whl 与 wheel-list.txt，
    # 否则会被打进交付包，成为"和项目无关的垃圾文件"）
    GPU_REQ_TMP="$(mktemp "${TMPDIR:-/tmp}/pdx-req-gpu.XXXXXX")"
    REQ_FILE="$GPU_REQ_TMP"
    grep -v -E '^[[:space:]]*paddlepaddle([<>=!~ ]|$)' "$PROJECT_ROOT/requirements.txt" > "$REQ_FILE"
    EXTRA_PKGS=("paddlepaddle-gpu==${PADDLE_VERSION}")
    # 同一目录只能有一种 Paddle：清掉上次 CPU 版残留（约 200 MB，避免装错/歧义）
    if [ -n "$(ls -1 "$DEST"/paddlepaddle-[0-9]*.whl 2>/dev/null || true)" ]; then
      warn "$DEST 里有 CPU 版 paddlepaddle 轮子，将被删除（CPU/GPU 轮子不能混放，否则安装时必装错）"
      warn "  若要同时保留 CPU 包，下次请分目录：--out offline/wheels-gpu"
    fi
    rm -f "$DEST"/paddlepaddle-[0-9]*.whl
    # 顺手清掉历史遗留的临时 requirements（老版本会写在这里）
    rm -f "$DEST"/.requirements-*.txt
  else
    rm -f "$DEST"/paddlepaddle_gpu-*.whl
  fi

  # 只有「本机就是 Linux 且架构一致」才能走原生解析；
  # macOS arm64 上 --arch arm64 的架构虽然"一致"，但下到的是 macOS 轮子，必须交叉下载。
  if [ "$ARCH" = "$(detect_arch)" ] && [ "$(uname -s)" = "Linux" ]; then
    info "当前机器为 Linux 且架构一致，直接解析依赖"
    "$PY" -m pip download \
      --only-binary=:all: \
      --python-version "$PY_VERSION" \
      --index-url "$PYPI_INDEX" \
      --extra-index-url "$PADDLE_INDEX" \
      -d "$DEST" \
      -r "$REQ_FILE" "${EXTRA_PKGS[@]}" \
      || die "依赖下载失败（${ARCH}）。若个别包无 wheel，请在该架构机器上执行本脚本 --native"
  else
    info "交叉下载（目标平台 linux_${TAG}）"
    "$PY" -m pip download \
      --only-binary=:all: \
      "${PLATFORM_ARGS[@]}" \
      --python-version "$PY_VERSION" \
      --implementation cp \
      --abi "cp$PY_VER" \
      --index-url "$PYPI_INDEX" \
      --extra-index-url "$PADDLE_INDEX" \
      -d "$DEST" \
      -r "$REQ_FILE" "${EXTRA_PKGS[@]}" \
      || die "交叉下载失败（${ARCH}）。某些包可能只提供源码包，请改在 $ARCH 机器上执行：./scripts/download_wheels.sh --arch $ARCH --native"
  fi

  # GPU 专属：补下 Linux 标记的 CUDA 依赖。
  # pip 在 macOS/arm64 上会按**当前解释器**判断 `platform_system == "Linux"` 标记，
  # 从而把 nvidia-* / cuda-python 全部跳过；到 x86_64 容器里安装就会报
  # "Could not find a version that satisfies nvidia-cuda-nvrtc-cu12==...".
  if [ "$DEVICE" = "gpu" ]; then
    PLAT_ARGS=("${PLATFORM_ARGS[@]}" --implementation cp --abi "cp$PY_VER")
    if [ "$ARCH" = "$(detect_arch)" ] && [ "$(uname -s)" = "Linux" ]; then
      PLAT_ARGS=()
    fi
    ROUND=1
    while [ "$ROUND" -le 6 ]; do
      MISSING="$("$PY" "$PROJECT_ROOT/scripts/gpu_deps.py" --dir "$DEST" --target-python "$PY_VERSION" 2>/dev/null || true)"
      [ -n "$MISSING" ] || break
      # 注意：不能只 read 一次——read 只读第一行，会导致每轮只补 1 个包
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

  # 关键包存在性校验
  if [ "$DEVICE" = "gpu" ]; then
    ls "$DEST"/paddlepaddle_gpu-*.whl >/dev/null 2>&1 \
      || die "$ARCH 缺少 paddlepaddle-gpu wheel（检查 Paddle 官方 ${CUDA} 源是否可访问）"
    [ -n "$GPU_REQ_TMP" ] && rm -f "$GPU_REQ_TMP"
  else
    ls "$DEST"/paddlepaddle-*.whl >/dev/null 2>&1 \
      || die "$ARCH 缺少 paddlepaddle wheel（检查 Paddle 官方源是否可访问）"
  fi
  ls "$DEST"/paddlex-*.whl >/dev/null 2>&1 || die "$ARCH 缺少 paddlex wheel"

  # 生成该架构的锁定清单，便于离线复现与审计
  ( cd "$DEST" && ls -1 *.whl | sort > wheel-list.txt )
  WHEEL_COUNT="$(wc -l < "$DEST/wheel-list.txt" | tr -d ' ')"
  SIZE="$(du -sh "$DEST" | awk '{print $1}')"
  ok "$ARCH 完成（${DEVICE}）：$WHEEL_COUNT 个 wheel，合计 $SIZE -> $DEST"
done

LAST_ARCH="${ARCHES[$((${#ARCHES[@]} - 1))]}"
OTHER_ARCH=amd64
[ "$LAST_ARCH" = "amd64" ] && OTHER_ARCH=arm64

# 打包命令要带上 wheel 目录：GPU 轮子通常放在 offline/wheels-gpu/<arch>，
# 与默认 offline/wheels/<arch> 不同，不带 --wheels-dir 会打到 CPU 轮子（或直接报缺包）。
case "$OUT_ROOT" in
  "$PROJECT_ROOT"/*) WHEELS_REL="${OUT_ROOT#"$PROJECT_ROOT"/}/$LAST_ARCH" ;;
  *)                 WHEELS_REL="$OUT_ROOT/$LAST_ARCH" ;;
esac
BUNDLE_ARGS="--arch ${LAST_ARCH} --wheels-dir ${WHEELS_REL}"
[ "$DEVICE" = "gpu" ] && BUNDLE_ARGS="$BUNDLE_ARGS --device gpu"

step "汇总"
for ARCH in "${ARCHES[@]}"; do
  printf '  %-6s %s 个包  %s\n' "$ARCH" "$(wc -l < "$OUT_ROOT/$ARCH/wheel-list.txt" | tr -d ' ')" "$(du -sh "$OUT_ROOT/$ARCH" | awk '{print $1}')"
done
if [ "$DEVICE" = "gpu" ]; then
  cat <<EOF

GPU 版已下载（${CUDA}）。目标机不需要显卡，但**运行机**需要：
  * NVIDIA 驱动 >= 575.57.08（cu129），\`nvidia-smi\` 正常；
  * glibc >= 2.17（随包 CPython 运行时的一致要求）。

下一步：
  1) 校验轮子（无需显卡）：./scripts/verify_gpu_wheel.sh --dir ${WHEELS_REL}
  2) 下载随包 Python 运行时：./scripts/download_python_runtime.sh --arch ${LAST_ARCH}
  3) 下载模型（与设备无关）：python3 scripts/download_models.py
  4) 打 GPU 离线包：./scripts/make_offline_bundle.sh ${BUNDLE_ARGS}
  5) 目标机安装：tar xzf dist/*-amd64-gpu.tar.gz && cd 解压目录
                 ./scripts/install_offline.sh --device gpu
                 ./scripts/preflight.sh
EOF
  exit 0
fi
cat <<EOF

下一步：
  1) 下载随包 Python 运行时（目标机没有 python3 时必需）：
       ./scripts/download_python_runtime.sh --arch ${LAST_ARCH}
     校验：./scripts/verify_runtime.sh --arch ${LAST_ARCH}
  2) 下载并打包模型（需要外网）：  python3 scripts/download_models.py --out models/paddlex_home
  3) 生成可拷贝到离线服务器的整包：./scripts/make_offline_bundle.sh ${BUNDLE_ARGS}
  4) 另一种架构（${OTHER_ARCH}）请再执行一次本脚本。
EOF
