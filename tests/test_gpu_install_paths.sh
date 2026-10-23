#!/usr/bin/env bash
# =============================================================================
# 回归测试：amd64 + NVIDIA GPU 的**本地部署**安装脚本分支（不需要显卡、不需要 6 GB 轮子）
#
# 背景：install_offline.sh 曾用 `paddlepaddle-*.whl` 校验 Paddle 轮子，
#       而 GPU 版文件名是 `paddlepaddle_gpu-*.whl`（下划线），
#       导致 GPU 包在目标机安装时 100% 报“缺少 paddlepaddle wheel”。
#
# 做法：用只含**空文件**的假 wheel 目录 + 容器里的真 python3.12，
#       跑 install_offline.sh --check-only（该阶段只做 glob/文件名匹配，不读 wheel 内容）。
#
# 用法：./tests/test_gpu_install_paths.sh
#   需要本机有 docker（镜像 python:3.12-slim-bookworm）；没有则自动跳过。
# =============================================================================
set -uo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${IMAGE:-python:3.12-slim-bookworm}"
PLATFORM="linux/amd64"
FAILED=0

check() {  # $1=名称 $2=0/1（0=通过）
  if [ "$2" -eq 0 ]; then
    printf '  [通过] %s\n' "$1"
  else
    FAILED=$((FAILED + 1))
    printf '  [失败] %s\n' "$1"
  fi
}

if ! command -v docker >/dev/null 2>&1; then
  echo "  [跳过] 没有 docker，本测试需要容器（只读挂载假 wheel 目录，秒级完成）"
  exit 0
fi
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "  [信息] 本地没有 $IMAGE，尝试拉取（离线环境可先 docker pull $IMAGE）"
  docker pull --platform "$PLATFORM" "$IMAGE" >/dev/null 2>&1 || {
    echo "  [跳过] 拉不到镜像 $IMAGE"
    exit 0
  }
fi

WORK="$(mktemp -d "${TMPDIR:-/tmp}/pdx-gpu-install.XXXXXX")"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

# 极简"项目骨架"：脚本只需要 scripts/、config/、requirements.txt 与 wheel 目录
mkdir -p "$WORK/proj/scripts" "$WORK/proj/config" "$WORK/proj/offline/wheels/amd64"
cp "$PROJECT_ROOT"/scripts/*.sh "$WORK/proj/scripts/"
cp "$PROJECT_ROOT"/scripts/gpu_deps.py "$WORK/proj/scripts/"
cp "$PROJECT_ROOT"/config/config.yaml "$WORK/proj/config/"
cp "$PROJECT_ROOT"/requirements.txt "$WORK/proj/"

GPU_DIR="$WORK/proj/offline/wheels/amd64"
# 只用文件名参与判定，内容为空即可（--check-only 不打开 wheel）
: > "$GPU_DIR/paddlepaddle_gpu-3.4.0-cp312-cp312-linux_x86_64.whl"
: > "$GPU_DIR/paddlex-3.7.2-py3-none-any.whl"
: > "$GPU_DIR/nvidia_cudnn_cu12-9.9.0.52-py3-none-manylinux_2_25_x86_64.whl"

run_install() {  # 剩余参数原样传给 install_offline.sh
  docker run --rm --platform "$PLATFORM" \
    -v "$WORK/proj:/app:ro" -w /app "$IMAGE" \
    bash scripts/install_offline.sh "$@" 2>&1
}

echo "1) GPU wheel 目录 + --device gpu 应通过预检"
OUT="$(run_install --check-only --device gpu --python /usr/local/bin/python3)"
RC=$?
check "退出码为 0" "$([ "$RC" -eq 0 ] && echo 0 || echo 1)"
printf '%s\n' "$OUT" | grep -q "找到 3 个 wheel" \
  && check "识别到 3 个 wheel" 0 || check "识别到 3 个 wheel" 1
printf '%s\n' "$OUT" | grep -q "缺少 paddlepaddle wheel" \
  && check "不再误报缺少 CPU 版 paddlepaddle" 1 || check "不再误报缺少 CPU 版 paddlepaddle" 0

echo "2) 同一目录混装 CPU/GPU 两版 Paddle 应被拦住"
: > "$GPU_DIR/paddlepaddle-3.4.0-cp312-cp312-linux_x86_64.whl"
OUT="$(run_install --check-only --device gpu --python /usr/local/bin/python3)"
RC=$?
check "混装时退出码非 0" "$([ "$RC" -ne 0 ] && echo 0 || echo 1)"
printf '%s\n' "$OUT" | grep -q "同时存在 CPU 版与 GPU 版" \
  && check "提示混装原因" 0 || check "提示混装原因" 1
rm -f "$GPU_DIR/paddlepaddle-3.4.0-cp312-cp312-linux_x86_64.whl"

echo "3) 只有 GPU 轮子却指定 --device cpu 应给出可操作提示"
OUT="$(run_install --check-only --device cpu --python /usr/local/bin/python3)"
RC=$?
check "退出码非 0" "$([ "$RC" -ne 0 ] && echo 0 || echo 1)"
printf '%s\n' "$OUT" | grep -q "只有 GPU 版 paddlepaddle_gpu 轮子" \
  && check "提示改用 --device gpu" 0 || check "提示改用 --device gpu" 1

echo "4) --device auto 应自动识别为 GPU"
OUT="$(run_install --check-only --python /usr/local/bin/python3)"
RC=$?
check "auto 模式退出码为 0" "$([ "$RC" -eq 0 ] && echo 0 || echo 1)"

echo
if [ "$FAILED" -eq 0 ]; then
  echo "全部通过"
  exit 0
fi
echo "失败 $FAILED 项"
exit 1
