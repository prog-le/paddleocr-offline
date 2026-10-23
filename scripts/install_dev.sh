#!/usr/bin/env bash
# =============================================================================
# 开发机 / 非 Linux 目标机上「先把服务跑起来」的安装脚本
#
# 与 scripts/install_offline.sh 的区别：
#   * install_offline.sh  = 生产用，Linux + 离线 wheel（paddle 3.4.0）
#   * install_dev.sh      = 本机联调用，在线装依赖（macOS 用 paddle 3.3.1）
#
# 用法：
#   ./scripts/install_dev.sh                 # 自动识别系统，创建 .venv 并安装
#   ./scripts/install_dev.sh --mirror        # 走中科大镜像（国内网络更快）
#   ./scripts/install_dev.sh --python /Users/xx/miniconda3/envs/ocr/bin/python
#   ./scripts/install_dev.sh --no-venv       # 装进当前 python（如已激活的 conda 环境）
#   ./scripts/install_dev.sh --with-mock-test # 安装后跑一次 MOCK 自检
# =============================================================================
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

MIRROR=0
NO_VENV=0
RUN_TEST=0
PYTHON_BIN="${PYTHON_BIN:-}"
VENV_DIR="${VENV_DIR:-$PROJECT_ROOT/.venv}"
MIRROR_URL="https://mirrors.ustc.edu.cn/pypi/simple"

while [ $# -gt 0 ]; do
  case "$1" in
    --mirror) MIRROR=1; shift ;;
    --python) PYTHON_BIN="$2"; shift 2 ;;
    --python=*) PYTHON_BIN="${1#*=}"; shift ;;
    --venv) VENV_DIR="$2"; shift 2 ;;
    --no-venv) NO_VENV=1; shift ;;
    --with-mock-test) RUN_TEST=1; shift ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) die "未知参数：$1" ;;
  esac
done

OS="$(uname -s)"
print_banner "开发环境安装（在线）"

case "$OS" in
  Darwin)
    REQ="$PROJECT_ROOT/requirements-macos.txt"
    info "检测到 macOS：使用 requirements-macos.txt（paddlepaddle 3.3.1）"
    ;;
  Linux)
    REQ="$PROJECT_ROOT/requirements.txt"
    info "检测到 Linux：使用 requirements.txt（paddlepaddle 3.4.0，与生产一致）"
    if [ "$(detect_arch)" = "arm64" ] || [ "$(detect_arch)" = "amd64" ]; then
      warn "Linux 生产部署请优先使用离线流程：scripts/download_wheels.sh + scripts/install_offline.sh"
    fi
    ;;
  *)
    die "不支持的系统：${OS}（本项目生产仅支持 Linux，x86_64/aarch64）"
    ;;
esac

step "1/3 准备 Python"
if [ -z "$PYTHON_BIN" ]; then
  for cand in python3.12 python3.11 python3.10 python3.13 python3; do
    command -v "$cand" >/dev/null 2>&1 && { PYTHON_BIN="$(command -v "$cand")"; break; }
  done
fi
[ -n "$PYTHON_BIN" ] || die "未找到 python3"
info "解释器：${PYTHON_BIN}（$("$PYTHON_BIN" -V 2>&1)）"

if [ "$NO_VENV" -eq 0 ]; then
  if [ ! -x "$VENV_DIR/bin/python" ]; then
    "$PYTHON_BIN" -m venv "$VENV_DIR" || die "创建虚拟环境失败"
    ok "已创建 $VENV_DIR"
  fi
  PY="$VENV_DIR/bin/python"
else
  PY="$PYTHON_BIN"
  info "使用当前解释器安装（--no-venv）"
fi
export VENV_PY="$PY"

step "2/3 安装依赖"
PIP_ARGS=(install -r "$REQ")
[ "$MIRROR" -eq 1 ] && PIP_ARGS+=(-i "$MIRROR_URL" --trusted-host mirrors.ustc.edu.cn)
"$PY" -m pip "${PIP_ARGS[@]}" || die "依赖安装失败（检查网络，或加 --mirror 换国内镜像）"
ok "依赖安装完成"

step "3/3 校验"
"$PY" - <<'PYEOF'
import sys
ok = True
for name in ("fastapi", "uvicorn", "yaml", "PIL", "docx", "openpyxl", "psutil", "paddle", "paddlex"):
    try:
        mod = __import__(name)
        print(f"  OK   {name:<10} {getattr(mod, '__version__', '?')}")
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"  FAIL {name:<10} {exc}")
if not ok:
    sys.exit(1)
PYEOF

if [ "$RUN_TEST" -eq 1 ]; then
  info "运行 MOCK 自检…"
  "$PY" "$PROJECT_ROOT/scripts/smoke_test.py" --mock | tail -8
fi

echo
ok "开发环境就绪"
cat <<EOF

启动方式：
  前台：  ${VENV_DIR}/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 --no-access-log
  或：    ./scripts/start.sh                 # 会优先使用 .venv
  仅看界面（不加载模型，MOCK 数据）：
          PADDLEX_OCR_MOCK=1 ./scripts/start.sh

注意：macOS 上跑的是 3.3.1，与生产（Linux + 3.4.0）仅版本差异，代码完全一致；
      正式交付验收请在目标 Linux 机器上执行 scripts/smoke_test.py（真实推理）。
EOF
