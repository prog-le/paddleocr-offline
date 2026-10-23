#!/usr/bin/env bash
# =============================================================================
# 容器入口：准备目录 -> 检查模型 -> 打印运行信息 -> 启动主进程
# 注意：此脚本不联网、不安装任何东西，只做本地检查与降权运行
# =============================================================================
set -euo pipefail

APP_DIR="${APP_DIR:-/app}"
DATA_DIR="${PADDLEX_OCR_DATA_DIR:-$APP_DIR/data}"
LOG_DIR="${PADDLEX_OCR_LOG_DIR:-$APP_DIR/logs}"
MODEL_DIR="${PADDLE_PDX_CACHE_HOME:-$APP_DIR/models/paddlex_home}/official_models"
PORT="${PORT:-8000}"

mkdir -p "$DATA_DIR" "$LOG_DIR" 2>/dev/null || true

echo "============================================================="
echo " PaddleX OCR Web · Docker 容器启动"
echo " 架构      : $(uname -m)"
echo " Python    : $(python -V 2>&1)"
echo " 数据目录  : $DATA_DIR"
echo " 日志目录  : $LOG_DIR"
echo " 模型目录  : $MODEL_DIR"
echo " 监听端口  : $PORT"
echo "============================================================="

if [ ! -d "$MODEL_DIR" ] || [ -z "$(ls -A "$MODEL_DIR" 2>/dev/null)" ]; then
  echo "[警告] 模型目录为空，识别任务将失败！" >&2
  echo "       请在构建镜像前执行 python3 scripts/download_models.py" >&2
else
  echo "[信息] 已内置模型 $(ls -1 "$MODEL_DIR" | wc -l | tr -d ' ') 个"
fi

# 打印容器内存上限：被 OOM kill 时这是最关键的一条信息
if [ -r /sys/fs/cgroup/memory.max ]; then
  MEM_MAX="$(cat /sys/fs/cgroup/memory.max 2>/dev/null || echo max)"
  MEM_CUR="$(cat /sys/fs/cgroup/memory.current 2>/dev/null || echo 0)"
elif [ -r /sys/fs/cgroup/memory/memory.limit_in_bytes ]; then
  MEM_MAX="$(cat /sys/fs/cgroup/memory/memory.limit_in_bytes 2>/dev/null || echo max)"
  MEM_CUR="$(cat /sys/fs/cgroup/memory/memory.usage_in_bytes 2>/dev/null || echo 0)"
else
  MEM_MAX="max"; MEM_CUR="0"
fi
if [ "$MEM_MAX" = "max" ]; then
  echo "[信息] 内存上限: 未限制（cgroup max）"
else
  echo "[信息] 内存上限: $((MEM_MAX / 1024 / 1024)) MB  已用: $((MEM_CUR / 1024 / 1024)) MB"
  if [ "$((MEM_MAX / 1024 / 1024))" -lt 4096 ]; then
    echo "[警告] 容器内存上限低于 4GB，识别大页面很可能被 OOM kill（表现为容器自动重启）。" >&2
    echo "       请调大 docker/.env 的 MEM_LIMIT，或降低 config.yaml 的 ocr.pdf_dpi / max_side_px。" >&2
  fi
fi

# 设备提示：GPU 镜像里如果看不到卡，99% 是没加 --gpus / 缺 nvidia-container-toolkit
if command -v nvidia-smi >/dev/null 2>&1; then
  if nvidia-smi -L >/dev/null 2>&1; then
    echo "[信息] NVIDIA GPU: $(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader | tr '\n' ';')"
    echo "[信息] 驱动版本: $(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1)"
  else
    echo "[警告] 检测到 nvidia-smi 但无法枚举 GPU：容器可能没有 --gpus all 或缺少 NVIDIA Container Toolkit。" >&2
    echo "       服务仍会以 CPU 运行（ocr.device=auto 时自动降级）。" >&2
  fi
fi
echo "[信息] 运行设备配置: PADDLEX_OCR_DEVICE=${PADDLEX_OCR_DEVICE:-auto}"

if [ ! -w "$DATA_DIR" ]; then
  echo "[错误] 数据目录不可写：${DATA_DIR}（检查 volume 挂载权限）" >&2
  exit 1
fi

# 首启动时若配置文件缺失则用默认配置兜底（正常构建已 COPY）
if [ ! -f "${PADDLEX_OCR_CONFIG:-$APP_DIR/config/config.yaml}" ]; then
  echo "[警告] 配置文件缺失：${PADDLEX_OCR_CONFIG:-$APP_DIR/config/config.yaml}" >&2
fi

exec "$@"
