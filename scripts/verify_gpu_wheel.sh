#!/usr/bin/env bash
# =============================================================================
# 校验 NVIDIA GPU 版 Paddle 轮子（**不需要显卡**，任意机器可跑）
#
# 用法：
#   ./scripts/verify_gpu_wheel.sh                       # 默认检查 offline/wheels/amd64
#   ./scripts/verify_gpu_wheel.sh --dir offline/wheels/amd64
#   ./scripts/verify_gpu_wheel.sh --wheel /path/paddlepaddle_gpu-3.4.0-cp312-cp312-linux_x86_64.whl
#
# 检查内容：
#   1. 文件名平台标签（linux_x86_64）与 cp 标签（cp312，需与目标 Python 一致）
#   2. 文件大小（paddlepaddle-gpu 3.4.0 约 3.35 GB；明显偏小说明下载不完整）
#   3. zip 完整性（CRC 校验，能发现截断/损坏）
#   4. CUDA 运行库来源：paddle 轮子只带头文件，运行库来自 nvidia-* pip 包；
#      本脚本会检查这些包是否已随轮子下载（缺了会在容器/离线安装时报错）
#   5. 打印 sha256，便于与下载记录/另一台机器比对
#
# 它**不能**验证 CUDA 是否真的可用——那需要 NVIDIA 驱动 + 显卡。
# 真正的推理验收请在目标机或临时 GPU 云主机执行：
#   ./scripts/install_offline.sh --device gpu && .venv/bin/python scripts/smoke_test.py
# =============================================================================
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

WHEEL=""
DIR="$PROJECT_ROOT/offline/wheels/amd64"

while [ $# -gt 0 ]; do
  case "$1" in
    --wheel) WHEEL="$2"; shift 2 ;;
    --wheel=*) WHEEL="${1#*=}"; shift ;;
    --dir) DIR="$2"; shift 2 ;;
    --dir=*) DIR="${1#*=}"; shift ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) die "未知参数：$1" ;;
  esac
done

PY="$(command -v python3 || true)"
[ -n "$PY" ] || die "需要 python3 来校验 wheel（构建机一般都有）"

if [ -z "$WHEEL" ]; then
  WHEEL="$(find "$DIR" -maxdepth 1 -name 'paddlepaddle_gpu-*.whl' 2>/dev/null | head -1 || true)"
fi
[ -n "$WHEEL" ] && [ -f "$WHEEL" ] || die "找不到 GPU 轮子。
     默认查找：$DIR/paddlepaddle_gpu-*.whl
     下载：./scripts/download_wheels.sh --arch amd64 --device gpu --cuda cu129"

print_banner "GPU 轮子校验（无需显卡）"
info "文件: $WHEEL"

FAILED=0
fail() { warn "$1"; FAILED=1; }

NAME="$(basename "$WHEEL")"
SIZE_B="$(wc -c < "$WHEEL" | tr -d ' ')"
SIZE_H="$(du -h "$WHEEL" | awk '{print $1}')"

# 1) 文件名标签
case "$NAME" in
  *linux_x86_64.whl) ok "平台标签：linux_x86_64" ;;
  *linux_aarch64.whl) fail "这是 aarch64 轮子，但官方并未发布 paddlepaddle-gpu 的 ARM 版本（文件来路可疑）" ;;
  *) fail "无法识别的平台标签：${NAME}（应为 linux_x86_64）" ;;
esac
CPTAG="$(echo "$NAME" | sed -nE 's/.*-(cp[0-9]+)-.*/\1/p')"
if [ -n "$CPTAG" ]; then
  info "Python 标签：${CPTAG}（必须与目标解释器一致，本项目为 cp312）"
else
  fail "文件名缺少 cpXX 标签"
fi

# 2) 体积（3.4.0/cu129 实测 3,345,865,395 字节）
MIN_B=$((2500 * 1024 * 1024))
if [ "$SIZE_B" -ge "$MIN_B" ]; then
  ok "体积：${SIZE_H}（可信）"
else
  fail "体积仅 ${SIZE_H}，明显小于预期（约 3.35 GB）——下载很可能不完整"
fi

# 3) zip 完整性 + 4) CUDA 库
if ! "$PY" - "$WHEEL" <<'PY'
import sys, zipfile

path = sys.argv[1]
try:
    zf = zipfile.ZipFile(path)
except Exception as exc:  # noqa: BLE001
    print(f"[失败] 不是有效的 wheel(zip)：{exc}")
    raise SystemExit(1)

bad = zf.testzip()
if bad:
    print(f"[失败] 压缩包损坏（首个坏条目：{bad}）")
    raise SystemExit(1)
print("[成功] zip 完整性校验通过")

names = zf.namelist()
# 真正的运行库是 .so；paddle 的 GPU 轮子通常只带 include/ 头文件，
# 运行库由 nvidia-* pip 包提供，因此这里只做事实陈述，不作为失败条件。
shared = [
    n
    for n in names
    if n.endswith((".so", ".so.12", ".so.9", ".so.1"))
    and ("/lib/" in n or n.startswith("paddle/libs/") or n.startswith("nvidia/"))
]
if shared:
    print(f"[成功] wheel 内置 {len(shared)} 个 .so（{shared[0]} 等）")
else:
    print("[信息] wheel 未内置 .so 运行库：CUDA/cuDNN 由 nvidia-* pip 包提供（见下方依赖检查）")
print(f"[信息] 条目数：{len(names)}")
PY
then
  fail "wheel 结构校验未通过"
fi

# 5) Linux 专属 CUDA 依赖是否已在同目录（这是容器构建失败的常见原因）
WHEEL_DIR="$(cd "$(dirname "$WHEEL")" && pwd)"
# python_version 条件按 wheel 的 cp 标签判断（cp312 -> 3.12），避免下载机解释器更老时漏判
TARGET_PY="3.12"
if [ -n "${CPTAG:-}" ]; then
  _NUM="${CPTAG#cp}"                     # cp312 -> 312，cp39 -> 39
  TARGET_PY="${_NUM:0:1}.${_NUM:1}"      # 312 -> 3.12
fi
MISSING="$("$PY" "$PROJECT_ROOT/scripts/gpu_deps.py" --dir "$WHEEL_DIR" --target-python "$TARGET_PY" 2>/dev/null || true)"
if [ -z "$MISSING" ]; then
  ok "Linux 专属 CUDA 依赖齐全（nvidia-* / cuda-python）"
else
  fail "缺少 Linux 专属 CUDA 依赖（离线安装/容器构建会失败）：
$(printf '%s\n' "$MISSING" | sed 's/^/       /')
     修复：./scripts/download_wheels.sh --arch amd64 --device gpu --cuda cu129 --out offline/wheels-gpu"
fi

# 6) sha256
if command -v shasum >/dev/null 2>&1; then
  SUM="$(shasum -a 256 "$WHEEL" | awk '{print $1}')"
else
  SUM="$(sha256sum "$WHEEL" | awk '{print $1}')"
fi
info "sha256: $SUM"
info "记录它，便于在目标机/另一台机器上比对同一文件"

cat <<EOF

驱动要求（cu129 轮子）：NVIDIA 驱动 >= 575.57.08（或 570 系列的 forward-compat 包）
容器运行：需要 nvidia-container-toolkit，并加 --gpus all（compose 见 docker-compose.gpu.yml）

下一步（打包这台机器不需要显卡）：
  1) ./scripts/make_offline_bundle.sh --arch amd64 --device gpu
  2) 目标机：./scripts/install_offline.sh --device gpu
             ./scripts/preflight.sh          # 第 2 步看 "设备 gpu:0 · <卡名>"
             .venv/bin/python scripts/smoke_test.py
EOF

if [ "$FAILED" -eq 1 ]; then
  die "校验未通过，请修复上面标出的问题（多数是下载不完整）"
fi
ok "GPU 轮子校验通过（注意：CUDA 是否可用仍需在带卡机器上验证）"
