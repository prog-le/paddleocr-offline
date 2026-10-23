#!/usr/bin/env bash
# =============================================================================
# 离线安装（在【无外网】的目标服务器上执行）
#
# 目标机不需要预装 python3：离线包内自带可重定位的 CPython 3.12 运行时
# （offline/python/<arch>/cpython-3.12.*-install_only.tar.gz），本脚本把它解包到
# <项目>/.runtime/python，用它创建 .venv，再从 offline/wheels/<arch> 离线装依赖。
#
# 前置：项目目录中已包含
#   offline/python/<arch>/*.tar.gz     （download_python_runtime.sh 生成）
#   offline/wheels/<arch>/*.whl        （download_wheels.sh --python 3.12 生成）
#
# 用法：
#   ./scripts/install_offline.sh                    # 用随包运行时（推荐，无需系统 python）
#   ./scripts/install_offline.sh --system-python    # 强制用系统 python3
#   ./scripts/install_offline.sh --python /usr/bin/python3.12
#   ./scripts/install_offline.sh --runtime /path/to/cpython-3.12.*-install_only.tar.gz
#   ./scripts/install_offline.sh --venv /opt/ocr-venv
#   ./scripts/install_offline.sh --check-only       # 只做环境检查，不安装
#   ./scripts/install_offline.sh --prepare-only     # 只解包运行时 + 建 venv + 备好 pip
#   ./scripts/install_offline.sh --device gpu       # 装 paddlepaddle-gpu（仅 amd64）
#   ./scripts/install_offline.sh --device gpu --wheels-dir offline/wheels-gpu/amd64
#                                                  # wheel 不在默认位置时指定（CPU/GPU 轮子
#                                                  #  分目录存放的常见用法）
# =============================================================================
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

PYTHON_BIN="${PYTHON_BIN:-}"
VENV_DIR="${VENV_DIR:-$PROJECT_ROOT/.venv}"
CHECK_ONLY=0
PREPARE_ONLY=0
FORCE_SYSTEM=0
RUNTIME_TARBALL_OPT=""
RT_TAG=""
DEVICE="auto"          # auto | cpu | gpu（决定装 paddlepaddle 还是 paddlepaddle-gpu）
WHEELS_DIR_OPT=""      # wheel 目录；默认 offline/wheels/<arch>

while [ $# -gt 0 ]; do
  case "$1" in
    --python) PYTHON_BIN="$2"; shift 2 ;;
    --python=*) PYTHON_BIN="${1#*=}"; shift ;;
    --runtime) RUNTIME_TARBALL_OPT="$2"; shift 2 ;;
    --runtime=*) RUNTIME_TARBALL_OPT="${1#*=}"; shift ;;
    --venv) VENV_DIR="$2"; shift 2 ;;
    --venv=*) VENV_DIR="${1#*=}"; shift ;;
    --wheels-dir) WHEELS_DIR_OPT="$2"; shift 2 ;;
    --wheels-dir=*) WHEELS_DIR_OPT="${1#*=}"; shift ;;
    --system-python) FORCE_SYSTEM=1; shift ;;
    --check-only) CHECK_ONLY=1; shift ;;
    --prepare-only) PREPARE_ONLY=1; shift ;;
    --device) DEVICE="$(echo "$2" | tr '[:upper:]' '[:lower:]')"; shift 2 ;;
    --device=*) DEVICE="$(echo "${1#*=}" | tr '[:upper:]' '[:lower:]')"; shift ;;
    -h|--help) sed -n '2,31p' "$0"; exit 0 ;;
    *) die "未知参数：$1" ;;
  esac
done

print_banner "离线依赖安装"

if [ "$(uname -s)" != "Linux" ] && [ "${PADDLEX_OCR_ALLOW_NONLINUX:-0}" != "1" ]; then
  warn "本脚本面向 Linux 离线部署，当前系统：$(uname -s)"
  warn "  * macOS 本地联调请执行：./scripts/install_dev.sh"
  warn "  * 本脚本只安装 offline/wheels 下的 Linux 轮子，在非 Linux 上必然失败"
  die "已中止（仅自测时可 set PADDLEX_OCR_ALLOW_NONLINUX=1 绕过系统检查）"
fi

ARCH="$(detect_arch)"
[ "$ARCH" != "unknown" ] || die "不支持的 CPU 架构：$(uname -m)"

# wheel 目录：--wheels-dir 优先（CPU/GPU 轮子分目录存放时的常见用法）
if [ -n "$WHEELS_DIR_OPT" ]; then
  WHEELS="$WHEELS_DIR_OPT"
  case "$WHEELS" in /*) ;; *) WHEELS="$PROJECT_ROOT/$WHEELS" ;; esac
else
  WHEELS="$PROJECT_ROOT/offline/wheels/$ARCH"
fi

# 设备变体：auto = 有 paddlepaddle-gpu 轮子就装 GPU 版，否则 CPU 版
if [ "$DEVICE" = "auto" ]; then
  if ls "$WHEELS"/paddlepaddle_gpu-*.whl >/dev/null 2>&1; then
    DEVICE="gpu"
  else
    DEVICE="cpu"
  fi
fi
case "$DEVICE" in
  cpu|gpu) ;;
  *) die "--device 只能是 auto / cpu / gpu" ;;
esac
if [ "$DEVICE" = "gpu" ] && [ "$ARCH" != "amd64" ]; then
  die "paddlepaddle-gpu 没有 $ARCH 轮子（官方只发 linux_x86_64）。
     ARM 服务器要用 NVIDIA GPU，请改用 NVIDIA NGC 的 paddle 镜像；
     或改用 CPU 版：./scripts/install_offline.sh --device cpu"
fi
if [ "$DEVICE" = "gpu" ] && ! ls "$WHEELS"/paddlepaddle_gpu-*.whl >/dev/null 2>&1; then
  die "$WHEELS 中没有 paddlepaddle-gpu 轮子。
     请在联网机器执行：./scripts/download_wheels.sh --arch amd64 --device gpu --cuda cu129 --out offline/wheels-gpu
     若轮子放在别的目录，用 --wheels-dir 指定：
       ./scripts/install_offline.sh --device gpu --wheels-dir offline/wheels-gpu/amd64"
fi

step "1/6 校验离线 wheel 包"
[ -d "$WHEELS" ] || die "缺少 $WHEELS —— 请在有外网的机器上执行 scripts/download_wheels.sh --arch $ARCH --python 3.12 后拷贝过来
     若 wheel 在自定义目录，用 --wheels-dir 指定（例如 --wheels-dir offline/wheels-gpu/amd64）"
COUNT="$(find "$WHEELS" -name '*.whl' | wc -l | tr -d ' ')"
[ "$COUNT" -gt 0 ] || die "$WHEELS 中没有 wheel 文件"
ok "找到 $COUNT 个 wheel（架构 $ARCH / $(wheel_arch)）"

# CPU 版与 GPU 版的轮子文件名不同（paddlepaddle-* / paddlepaddle_gpu-*），
# 必须按设备分别校验，否则 GPU 目录会被 CPU 版的 glob 判成"缺包"。
if [ "$DEVICE" = "gpu" ]; then
  ls "$WHEELS"/paddlepaddle_gpu-*.whl >/dev/null 2>&1 \
    || die "缺少 paddlepaddle-gpu wheel（$WHEELS），请重新下载"
  if ls "$WHEELS"/paddlepaddle-[0-9]*.whl >/dev/null 2>&1; then
    die "$WHEELS 里同时存在 CPU 版与 GPU 版 Paddle 轮子（装哪个都不对）。
     请重新下载 GPU 轮子（脚本会自动清掉 CPU 版残留）：
       ./scripts/download_wheels.sh --arch amd64 --device gpu --cuda cu129 --out offline/wheels-gpu"
  fi
else
  if ! ls "$WHEELS"/paddlepaddle-*.whl >/dev/null 2>&1; then
    if ls "$WHEELS"/paddlepaddle_gpu-*.whl >/dev/null 2>&1; then
      die "$WHEELS 里只有 GPU 版 paddlepaddle_gpu 轮子，但当前是 --device cpu。
     装 GPU 版：  ./scripts/install_offline.sh --device gpu
     要 CPU 版：  ./scripts/download_wheels.sh --arch $ARCH --python 3.12   # 下载 CPU 轮子"
    fi
    die "缺少 paddlepaddle wheel，请重新下载"
  fi
fi
ls "$WHEELS"/paddlex-*.whl >/dev/null 2>&1 || die "缺少 paddlex wheel，请重新下载"

WHEEL_CPTAG="$(ls -1 "$WHEELS" 2>/dev/null | grep -oE 'cp3[0-9]+' | head -1 || true)"
[ -n "$WHEEL_CPTAG" ] && info "wheel 的 Python 标签：$WHEEL_CPTAG"

step "2/6 准备 Python 运行时"

extract_runtime() {  # $1=tarball
  local tarball="$1" dst tmp
  dst="$(runtime_dir)"
  [ -f "$tarball" ] || die "运行时压缩包不存在：$tarball"
  info "解包运行时：$(basename "$tarball")"
  mkdir -p "$(dirname "$dst")"
  tmp="$(mktemp -d "$(dirname "$dst")/.unpack.XXXXXX")"
  # install_only 包顶层目录固定为 python/
  if ! tar xzf "$tarball" -C "$tmp"; then
    rm -rf "$tmp"
    die "解包失败：${tarball}（文件可能损坏，可核对同目录的 .sha256）"
  fi
  if [ ! -x "$tmp/python/bin/python3" ]; then
    warn "解包后的顶层内容："
    ls -1 "$tmp" >&2 || true
    rm -rf "$tmp"
    die "运行时包结构异常：缺少 python/bin/python3"
  fi
  rm -rf "$dst"
  mv "$tmp/python" "$dst"
  rmdir "$tmp" 2>/dev/null || true
  ok "运行时已就位：$dst"
}

if [ -n "$PYTHON_BIN" ]; then
  [ -x "$PYTHON_BIN" ] || die "--python 指定的解释器不可执行：$PYTHON_BIN"
  info "使用 --python 指定的解释器：$PYTHON_BIN"
elif [ "$FORCE_SYSTEM" -eq 1 ]; then
  PYTHON_BIN="$(command -v python3 || true)"
  [ -n "$PYTHON_BIN" ] || die "已指定 --system-python，但系统没有 python3"
  info "使用系统 python3：$PYTHON_BIN"
elif EXISTING_RT="$(runtime_python)"; then
  PYTHON_BIN="$EXISTING_RT"
  ok "复用已解包的随包运行时：$PYTHON_BIN"
else
  RT_PACK="$RUNTIME_TARBALL_OPT"
  [ -n "$RT_PACK" ] || RT_PACK="$(runtime_tarball "$ARCH" || true)"
  if [ -n "$RT_PACK" ] && [ -f "$RT_PACK" ]; then
    if [ "$CHECK_ONLY" -eq 0 ]; then
      extract_runtime "$RT_PACK"
      PYTHON_BIN="$(runtime_python || true)"
    else
      # 检查模式下不解包（Linux 二进制在本机也未必能跑），按文件名判定版本
      RT_TAG="$(basename "$RT_PACK" | sed -E 's/^cpython-([0-9]+[.][0-9]+)[.].*/\1/')"
      info "检查模式：不解包，按包名判定运行时为 Python ${RT_TAG:-未知}"
      info "          压缩包：$(basename "$RT_PACK")"
      PYTHON_BIN="$(runtime_dir)/bin/python3"
    fi
    [ -n "$PYTHON_BIN" ] || die "解包后未找到 $(runtime_dir)/bin/python3"
  else
    PYTHON_BIN="$(command -v python3 || true)"
    if [ -z "$PYTHON_BIN" ]; then
      warn "离线包里没有运行时（offline/python/$ARCH/*.tar.gz），系统也没有 python3"
      die "无法继续。请在联网机器执行 ./scripts/download_python_runtime.sh --arch $ARCH 后重新打包"
    fi
    warn "未找到随包运行时，退回系统 python3：$PYTHON_BIN"
    warn "（目标机若没有 python3，请把 offline/python/$ARCH/*.tar.gz 一起打包）"
  fi
fi

[ -n "$PYTHON_BIN" ] || die "未能确定 Python 解释器"

# 运行时能否真正跑起来（老 glibc 会报 GLIBC_2.xx not found）
if [ -z "${RT_TAG:-}" ] && { [ "$CHECK_ONLY" -eq 0 ] || [ -x "$PYTHON_BIN" ]; }; then
  if ! "$PYTHON_BIN" -c 'import sys' >/dev/null 2>&1; then
    ERR="$("$PYTHON_BIN" -c 'import sys' 2>&1 || true)"
    die "解释器无法运行：$PYTHON_BIN
     ${ERR}
     若报 GLIBC_x.xx not found，说明本运行时要求的最低 glibc 高于本机（正常需 glibc ≥ 2.17）。
     可改用系统 python3（--system-python），或换一份更老的运行时。"
  fi
fi

if [ -n "${RT_TAG:-}" ]; then
  PYVER="$RT_TAG"
  info "运行时（包内，未解包）: Python $PYVER"
else
  PYVER="$(python_version_of "$PYTHON_BIN")"
  info "解释器: $PYTHON_BIN (Python $PYVER)"
fi
case "$PYVER" in
  3.10|3.11|3.12|3.13) ;;
  *) die "Python $PYVER 不受支持：paddlepaddle 3.4.0 仅提供 cp310~cp313 的 linux wheel" ;;
esac

# wheel 的 cp 标签必须与解释器一致，否则离线安装必然失败（常见于下载时版本填错）
CPTAG="cp${PYVER/./}"
if ! ls "$WHEELS" 2>/dev/null | grep -q -- "$CPTAG"; then
  warn "在 $WHEELS 中没找到 $CPTAG 的 wheel（现有标签：${WHEEL_CPTAG:-未知}）。"
  warn "说明依赖是用别的 Python 版本下载的（例如下载机 --python 3.10，运行时是 3.12）。"
  warn "解决办法：在有网机器执行  ./scripts/download_wheels.sh --arch $ARCH --python $PYVER  后重新打包。"
  die "Python 版本与 wheel 不匹配"
fi
ok "wheel 的 Python 标签匹配：$CPTAG"

if [ "$CHECK_ONLY" -eq 1 ]; then
  step "3/6 检查模式结束"
  ok "运行时 / wheel / 版本匹配检查通过（未做任何改动）"
  exit 0
fi

step "3/6 创建虚拟环境"
if [ -d "$VENV_DIR" ] && [ -x "$VENV_DIR/bin/python" ]; then
  info "复用已存在的虚拟环境：$VENV_DIR"
  OLD_VER="$(python_version_of "$VENV_DIR/bin/python")"
  if [ "$OLD_VER" != "$PYVER" ]; then
    warn "已有 venv 的 Python 是 ${OLD_VER}，与运行时 $PYVER 不一致，重建"
    rm -rf "$VENV_DIR"
  fi
fi
if [ ! -x "$VENV_DIR/bin/python" ]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR" \
    || die "创建虚拟环境失败（用系统 python 时 Debian/Ubuntu 需先装 python3-venv）"
  ok "已创建 ${VENV_DIR}（基于 ${PYTHON_BIN}）"
fi
VENV_PY="$VENV_DIR/bin/python"
export VENV_PY

if ! "$VENV_PY" -m pip --version >/dev/null 2>&1; then
  info "venv 中缺少 pip，尝试 ensurepip"
  if ! "$VENV_PY" -m ensurepip --upgrade >/dev/null 2>&1; then
    # 兜底：直接用离线 wheel 里的 pip（zipimport 方式运行，不需要已装 pip）
    PIP_WHL="$(ls -1 "$WHEELS"/pip-*.whl 2>/dev/null | head -1 || true)"
    if [ -n "$PIP_WHL" ]; then
      info "ensurepip 失败，改用离线 pip wheel：$(basename "$PIP_WHL")"
      "$VENV_PY" "$PIP_WHL/pip" install --no-index --no-cache-dir --find-links "$WHEELS" pip \
        >/dev/null 2>&1 || die "无法准备 pip（ensurepip 与离线 pip wheel 都失败）"
    else
      die "无法准备 pip：ensurepip 失败且 $WHEELS 中没有 pip-*.whl（请在下载机加装 pip 到 wheel 目录）"
    fi
  fi
fi
"$VENV_PY" -m pip --version >/dev/null 2>&1 || die "pip 仍不可用"

if [ "$PREPARE_ONLY" -eq 1 ]; then
  step "4/6 准备模式结束"
  ok "运行时与虚拟环境已就绪（未安装依赖）：$VENV_PY"
  info "后续可执行：$VENV_PY -m pip install --no-index --find-links $WHEELS -r requirements.txt"
  exit 0
fi

# GPU：先确认 Linux 专属 CUDA 依赖齐全（nvidia-* / cuda-python）。
# 少一个都会在安装到一半时报 "No matching distribution found"，这里提前拦住。
if [ "$DEVICE" = "gpu" ]; then
  MISSING_DEPS="$("$VENV_PY" "$PROJECT_ROOT/scripts/gpu_deps.py" --dir "$WHEELS" --target-python "$PYVER" 2>/dev/null || true)"
  if [ -n "$MISSING_DEPS" ]; then
    die "离线包缺少 GPU 运行库依赖（在 x86_64 容器/机器上会装不上）：
$(printf '%s\n' "$MISSING_DEPS" | sed 's/^/     - /')
     请在联网机器执行（建议用清华镜像，USTC 缺少 12.9.x 的 nvidia 包）：
       PYPI_INDEX=https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple \
       ./scripts/download_wheels.sh --arch amd64 --device gpu --cuda cu129 --out offline/wheels-gpu
     然后重新打包。"
  fi
  ok "CUDA 运行库依赖齐全"
fi

step "4/6 安装依赖（完全离线，--no-index，设备 ${DEVICE}）"
info "pip 将只从 $WHEELS 取包，不访问任何网络"
export PIP_NO_INDEX=1
REQ_FILE="$PROJECT_ROOT/requirements.txt"
GPU_REQ_TMP=""
if [ "$DEVICE" = "gpu" ]; then
  # requirements.txt 里是 CPU 版 paddlepaddle，必须剔除，否则会和 GPU 版冲突。
  # 写临时文件而不是写进 $WHEELS：wheel 目录可能是只读挂载（容器/U 盘）。
  GPU_REQ_TMP="$(mktemp "${TMPDIR:-/tmp}/pdx-req-gpu.XXXXXX")"
  REQ_FILE="$GPU_REQ_TMP"
  grep -v -E '^[[:space:]]*paddlepaddle([<>=!~ ]|$)' "$PROJECT_ROOT/requirements.txt" > "$REQ_FILE"
fi
if ! "$VENV_PY" -m pip install --no-index --find-links "$WHEELS" --disable-pip-version-check \
      -r "$REQ_FILE"; then
  [ -n "$GPU_REQ_TMP" ] && rm -f "$GPU_REQ_TMP"
  die "离线安装失败。常见原因：wheel 架构不匹配（当前 $(uname -m)）、Python 版本不匹配（${PYVER}）、wheel 缺包"
fi
if [ "$DEVICE" = "gpu" ]; then
  info "安装 GPU 版 Paddle：$(basename "$(ls -1 "$WHEELS"/paddlepaddle_gpu-*.whl | head -1)")"
  if ! "$VENV_PY" -m pip install --no-index --find-links "$WHEELS" --disable-pip-version-check \
        paddlepaddle-gpu; then
    [ -n "$GPU_REQ_TMP" ] && rm -f "$GPU_REQ_TMP"
    die "paddlepaddle-gpu 安装失败（轮子与 Python ${PYVER} 或 glibc 不匹配？）"
  fi
fi
[ -n "$GPU_REQ_TMP" ] && rm -f "$GPU_REQ_TMP"
ok "依赖安装完成（${DEVICE}）"

step "5/6 校验安装结果"
if [ "$DEVICE" = "gpu" ]; then
  # 打包机/无卡机器上没有 NVIDIA 驱动，import paddle 会因 dlopen libcuda.so.1 失败。
  # 这是 GPU 版的预期现象，不应让安装判定为失败（真正的验收在带卡机器上做）。
  export PADDLEX_OCR_ALLOW_NO_DRIVER=1
fi
if ! "$VENV_PY" - <<'PY'
import os
import sys

allow_no_driver = os.environ.get("PADDLEX_OCR_ALLOW_NO_DRIVER") == "1"
need = ("fastapi", "uvicorn", "yaml", "PIL", "docx", "openpyxl", "psutil", "paddle", "paddlex")

failed = False
paddle_loaded = False
for name in need:
    try:
        mod = __import__(name)
        print(f"  OK    {name:<10} {getattr(mod, '__version__', '?')}")
        if name == "paddle":
            paddle_loaded = True
    except Exception as exc:  # noqa: BLE001
        msg = (str(exc).strip().splitlines() or [exc.__class__.__name__])[0]
        if name == "paddle" and allow_no_driver and "libcuda" in msg:
            # 已装 GPU 版且解释器能加载，只是本机没有驱动
            print(f"  WARN  {name:<10} 加载失败：找不到 libcuda.so.1（本机无 NVIDIA 驱动）")
            continue
        print(f"  FAIL  {name:<10} {msg}")
        failed = True
if failed:
    print("\n  修复建议：", file=sys.stderr)
    print("    * 缺模块 -> 重新执行 ./scripts/install_offline.sh", file=sys.stderr)
    print("    * paddle/paddlex 版本冲突 -> 检查 wheel 目录是否混装了 CPU/GPU 两版 Paddle", file=sys.stderr)
    sys.exit(1)

print(f"  解释器: {sys.executable} ({sys.version.split()[0]})")
if not paddle_loaded:
    print("  [警告] GPU 版 paddle 已安装，但本机没有 NVIDIA 驱动，跳过 CUDA 能力检测。")
    print("         请在带卡的服务器上执行 ./scripts/preflight.sh 与 scripts/smoke_test.py 验收。")
    sys.exit(0)

import paddle  # noqa: E402  （上面已确认可导入）

has_cuda = paddle.device.is_compiled_with_cuda()
print(f"  paddle 版本: {paddle.__version__}  编译特性: cuda={has_cuda}")
if has_cuda:
    try:
        n = paddle.device.cuda.device_count()
        print(f"  可见 GPU: {n} 张")
        for i in range(n):
            p = paddle.device.cuda.get_device_properties(i)
            total = int(getattr(p, "total_memory", 0)) // (1024 * 1024)
            print(f"    GPU{i}: {getattr(p, 'name', '?')}  {total}MB")
        if n == 0:
            print("  [警告] GPU 版 paddle 已装，但当前进程看不到 GPU：")
            print("         宿主机需安装 NVIDIA 驱动（>=575.57.08 for cu129）")
            print("         （容器还需 NVIDIA Container Toolkit + --gpus all）")
    except Exception as exc:  # noqa: BLE001
        print(f"  [警告] 查询 GPU 信息失败：{exc}")
PY
then
  die "依赖校验失败，请查看上面的 FAIL 项"
fi

step "6/6 收尾"
mkdir -p "$PROJECT_ROOT/data" "$PROJECT_ROOT/logs" "$(model_home)/official_models"

cat > "$PROJECT_ROOT/.venv_path" <<EOF
$VENV_DIR
EOF

# 记录运行时来源，便于排障（preflight 会读）
{
  echo "python_bin=$PYTHON_BIN"
  echo "python_version=$PYVER"
  echo "venv=$VENV_DIR"
  echo "arch=$ARCH"
  echo "device=$DEVICE"
  echo "installed_at=$(date '+%Y-%m-%d %H:%M:%S')"
} > "$PROJECT_ROOT/.runtime_env"

case "$PYTHON_BIN" in
  "$PROJECT_ROOT"/.runtime/*) RT_KIND="随包运行时" ;;
  *)                          RT_KIND="系统 python3" ;;
esac

ok "离线安装完成（Python ${PYVER}，${RT_KIND}，Paddle ${DEVICE} 版）"
if [ "$DEVICE" = "gpu" ]; then
  info "wheel 来源：$WHEELS"
  info "启用 GPU：确认 config/config.yaml 的 ocr.device 为 auto 或 gpu:0，然后 ./scripts/start.sh"
  info "无卡机器上 import paddle 会因 libcuda 失败属正常；请在带卡机器执行 preflight + smoke_test"
  info "显存不足时调小 ocr.text_rec_batch_size / ocr.pdf_dpi，或用 ocr.gpu_mem_fraction 限流"
fi
cat <<EOF

后续步骤：
  1. 确认模型已就位：  ls $(model_home)/official_models
  2. 环境自检：        ./scripts/preflight.sh
  3. 启动服务：        ./scripts/start.sh            # 前台运行
                       ./scripts/start.sh --daemon   # 后台运行
  4. 访问：            http://<服务器IP>:$(config_get server.port 8000)
  5. 管理后台：        http://<服务器IP>:$(config_get server.port 8000)/admin
EOF
