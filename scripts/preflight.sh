#!/usr/bin/env bash
# =============================================================================
# 环境自检：不影响运行，只做体检并给出结论（可在离线服务器上执行）
# 用法: ./scripts/preflight.sh
# =============================================================================
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

print_banner "环境自检"

FAIL=0
note_fail() { warn "$*"; FAIL=$((FAIL+1)); }

step "1/8 系统与架构"
info "内核: $(uname -srm)"
case "$(detect_arch)" in
  amd64|arm64) ok "CPU 架构受支持：$(detect_arch)（wheel 标签 $(wheel_arch)）" ;;
  *) note_fail "不支持的架构：$(uname -m)，本项目仅支持 x86_64 / aarch64" ;;
esac
if [ -f /etc/os-release ]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  info "发行版: ${PRETTY_NAME:-unknown}"
fi
# glibc：随包运行时只要求 2.17，但** wheel 里有 manylinux_2_28 的包**
# （numpy / pillow / PyMuPDF / scipy 等），pip 在 glibc < 2.28 的机器上会直接拒绝安装。
if [ "$(uname -s)" = "Linux" ]; then
  GLIBC_V="$(getconf GNU_LIBC_VERSION 2>/dev/null | awk '{print $2}' || true)"
  [ -n "$GLIBC_V" ] || GLIBC_V="$(ldd --version 2>/dev/null | head -1 | grep -oE '[0-9]+\.[0-9]+$' || true)"
  if [ -n "$GLIBC_V" ]; then
    if awk -v v="$GLIBC_V" 'BEGIN{exit !(v + 0 >= 2.28)}'; then
      ok "glibc $GLIBC_V（≥ 2.28，满足 wheel 的 manylinux_2_28 标签）"
    else
      note_fail "glibc $GLIBC_V < 2.28：离线 wheel 含 manylinux_2_28 包（numpy/pillow/PyMuPDF 等），pip 会拒绝安装。
     请换用 Ubuntu 20.04+ / Debian 10+ / RHEL 8+ 及同等 glibc 的系统"
    fi
  fi
fi

# NVIDIA 驱动：GPU 部署的硬门槛（cu129 轮子要求 >= 575.57.08）
GPU_INSTALLED=0
if [ -f "$PROJECT_ROOT/.runtime_env" ]; then
  case "$(cat "$PROJECT_ROOT/.runtime_env" 2>/dev/null)" in
    *device=gpu*) GPU_INSTALLED=1 ;;
  esac
fi
if [ "$GPU_INSTALLED" -eq 1 ]; then
  if command -v nvidia-smi >/dev/null 2>&1; then
    DRV="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1 | tr -d ' ')"
    if [ -n "$DRV" ]; then
      if awk -v v="$DRV" 'BEGIN{n=split(v,a,"."); cur=a[1]*10000+(a[2]+0)*100+(a[3]+0);
                              exit !(cur >= 575*10000 + 57*100 + 8)}'; then
        ok "NVIDIA 驱动 $DRV（≥ 575.57.08，满足 CUDA 12.9 轮子）"
      else
        warn "NVIDIA 驱动 $DRV 低于 575.57.08：CUDA 12.9 轮子可能报 'CUDA driver version is insufficient'"
      fi
    fi
    nvidia-smi --query-gpu=index,name,memory.total --format=csv,noheader 2>/dev/null | head -4 | \
      while IFS= read -r line; do info "显卡: $line"; done
  else
    warn "已安装 GPU 版 Paddle，但本机没有 nvidia-smi"
    warn "  若本机就是要跑推理的服务器：先装 NVIDIA 驱动（>=575.57.08 for cu129）"
    warn "  若本机只是打包机：忽略此提示，真正验收在带卡机器上做"
  fi
fi

step "2/8 Python（运行时）"
PY="$(resolve_python || true)"
if [ -z "$PY" ]; then
  note_fail "未找到 Python：既没有随包运行时，也没有系统 python3"
  info "  修复：把 offline/python/<target>/ 下的运行时压缩包放进离线包，执行 ./scripts/install_offline.sh"
else
  info "解释器: $PY"
  VER="$(python_version_of "$PY")"
  case "$VER" in
    3.10|3.11|3.12|3.13) ok "Python $VER" ;;
    *) note_fail "Python $VER 不受支持，离线 wheel 仅提供 cp310~cp313" ;;
  esac

  # 运行时来源：venv 的 base_prefix 指向 .runtime 即为随包运行时
  BASE_PREFIX="$("$PY" -c 'import sys;print(sys.base_prefix)' 2>/dev/null || true)"
  case "$BASE_PREFIX" in
    "$PROJECT_ROOT"/.runtime/*) ok "运行时来源：随包（${BASE_PREFIX}）" ;;
    *) info "运行时来源：系统或外部解释器（base_prefix=${BASE_PREFIX:-?}）" ;;
  esac

  # 随包运行时压缩包（未解包时提示）
  if runtime_python >/dev/null 2>&1; then :; else
    RT_PACK="$(runtime_tarball "$(target_id)" || true)"
    if [ -n "$RT_PACK" ]; then
      info "离线包内运行时：$(basename "$RT_PACK")（尚未解包，install_offline.sh 会自动处理）"
    else
      warn "离线包内没有运行时（$(target_runtime_dir "$(target_id)")/*.tar.gz）"
      warn "  目标机若无 python3 将无法安装：请在联网机器执行 ./scripts/download_python_runtime.sh"
    fi
  fi

  if "$PY" -c "import sys;print(sys.prefix)" >/dev/null 2>&1; then
    if "$PY" -c 'import sys;raise SystemExit(0 if sys.prefix!=sys.base_prefix else 1)'; then
      info "虚拟环境: 是（$(dirname "$(dirname "$PY")")）"
    else
      info "虚拟环境: 否（直接用解释器，未建 venv）"
    fi
  fi

  # 运行设备：CPU / NVIDIA GPU 是否真的可用（这是"装了 GPU 版却没生效"的常见坑）
  DEV_LINE="$("$PY" - "$PROJECT_ROOT" <<'PY' 2>/dev/null || true
import sys
sys.path.insert(0, sys.argv[1])
from backend.settings import Settings
from backend.devices import summary
d = summary(Settings())
a = d.get("active") or {}
caps = d.get("capabilities") or {}
b = d.get("batching") or {}
gpus = d.get("gpus") or []
ok = a.get("kind") != "gpu" or caps.get("cuda")
head = "OK" if ok else "FAIL"
print(f"{head}|设备 {a.get('device')} · {a.get('name')}（配置 {d.get('configured')}，CUDA={caps.get('cuda')}）")
print(f"INFO|OCR 档位 {d.get('ocr_tier')} / 识别 batch {b.get('text_rec_batch_size')} / CPU 线程 {d.get('cpu_threads')}")
for g in gpus:
    print(f"INFO|GPU{g.get('index')}: {g.get('name')} {g.get('total_mem_mb') or '?'}MB")
if a.get("reason"):
    print(f"INFO|说明：{a['reason']}")
PY
)"
  if [ -n "$DEV_LINE" ]; then
    while IFS= read -r line; do
      case "$line" in
        OK\|*)   ok "${line#OK|}" ;;
        FAIL\|*) note_fail "${line#FAIL|}" ;;
        INFO\|*) info "${line#INFO|}" ;;
      esac
    done <<< "$DEV_LINE"
    if grep -q '^FAIL|' <<< "$DEV_LINE"; then
      info "  修复：./scripts/download_wheels.sh --os linux --arch amd64 --device gpu --cuda cu129"
      info "        ./scripts/install_offline.sh --device gpu  （并确认宿主机 nvidia-smi 正常）"
    fi
  else
    warn "无法读取设备信息（依赖是否已安装？）"
  fi

  # wheel 的 cp 标签必须与解释器一致
  WHEELS_DIR="$(target_wheels_dir "$(target_id)")"
  if [ -d "$WHEELS_DIR" ]; then
    TAG="$(ls -1 "$WHEELS_DIR" 2>/dev/null | grep -oE 'cp3[0-9]+' | head -1 || true)"
    if [ -n "$TAG" ]; then
      if [ "$TAG" = "cp${VER/./}" ]; then
        ok "wheel 标签与解释器匹配：$TAG"
      else
        note_fail "wheel 标签 ${TAG} 与解释器 Python ${VER} 不一致（需重新下载 --python ${VER}）"
      fi
    fi
  fi
fi

step "3/8 依赖可用性"
if [ -n "$PY" ]; then
  for mod in fastapi uvicorn yaml PIL docx openpyxl psutil; do
    if "$PY" -c "import $mod" >/dev/null 2>&1; then
      info "OK   $mod"
    else
      note_fail "缺失 ${mod}（请执行 ./scripts/install_offline.sh）"
    fi
  done
  # PDF 渲染采用三级降级，至少要有一个可用（都是 wheel，离线可装）
  PDF_OK=0
  for mod in pymupdf fitz pypdfium2 pdf2image; do
    if "$PY" -c "import ${mod}" >/dev/null 2>&1; then
      info "OK   PDF 渲染：${mod}"
      PDF_OK=1
    fi
  done
  [ "$PDF_OK" -eq 1 ] && ok "PDF 渲染链路可用（PyMuPDF / pypdfium2 / poppler 任一）" \
    || note_fail "PDF 渲染依赖全缺（pymupdf / pypdfium2 / pdf2image 至少装一个，否则 PDF 无法识别）"

  # paddle：区分「没装」与「装了但加载不到 CUDA 运行库」（后者在无卡机器上是正常的）
  PADDLE_DIST="$("$PY" - <<'PY' 2>/dev/null || true
import importlib.metadata as m
for d in ("paddlepaddle-gpu", "paddlepaddle"):
    try:
        print(f"{d} {m.version(d)}")
        break
    except Exception:
        continue
PY
)"
  if PADDLE_ERR="$("$PY" -c 'import paddle' 2>&1)"; then
    ok "paddle: $("$PY" -c 'import paddle;print(paddle.__version__, "cuda" if paddle.device.is_compiled_with_cuda() else "cpu")' 2>/dev/null)（${PADDLE_DIST:-未知发行版}）"
  else
    case "$PADDLE_ERR" in
      *libcuda*|*libcudart*|*libcudnn*|*libcublas*)
        warn "paddle 已安装（${PADDLE_DIST:-未知}），但加载不到 CUDA 运行库："
        warn "  $(printf '%s\n' "$PADDLE_ERR" | head -1)"
        warn "  带卡机器：确认 NVIDIA 驱动 ≥575.57.08（cu129）且 nvidia-smi 正常；"
        warn "  无卡机器：这是 GPU 版的预期现象（打包机常见），可忽略"
        ;;
      *"No module named"*|*ModuleNotFoundError*)
        note_fail "未安装 paddle（CPU 版 paddlepaddle / GPU 版 paddlepaddle-gpu 都没有）"
        ;;
      *)
        note_fail "import paddle 失败：$(printf '%s\n' "$PADDLE_ERR" | head -1)"
        ;;
    esac
  fi
  if "$PY" -c "import paddlex" >/dev/null 2>&1; then
    ok "paddlex: $("$PY" -c 'import paddlex;print(paddlex.__version__)' 2>/dev/null)"
  else
    note_fail "未安装 paddlex"
  fi
  for mod in fitz pypdfium2 pdf2image; do
    if "$PY" -c "import $mod" >/dev/null 2>&1; then
      ok "PDF 渲染后端可用：$mod"
    fi
  done
  if ! "$PY" -c "import fitz" >/dev/null 2>&1 && ! "$PY" -c "import pypdfium2" >/dev/null 2>&1 && ! command -v pdftoppm >/dev/null 2>&1; then
    note_fail "无可用 PDF 渲染后端（需 PyMuPDF 或 pypdfium2 或 poppler-utils）"
  fi
fi

step "4/8 模型文件"
MODELS="$(model_home)/official_models"
if [ -d "$MODELS" ]; then
  COUNT="$(find "$MODELS" -mindepth 1 -maxdepth 1 -type d | wc -l | tr -d ' ')"
  if [ "$COUNT" -gt 0 ]; then
    ok "模型目录存在，共 $COUNT 个模型：$MODELS"
    find "$MODELS" -mindepth 1 -maxdepth 1 -type d -printf '     - %f\n' 2>/dev/null \
      || find "$MODELS" -mindepth 1 -maxdepth 1 -type d -exec basename {} \; | sed 's/^/     - /'
  else
    note_fail "模型目录为空：${MODELS}（联网机器上执行 scripts/download_models.py 后拷贝过来）"
  fi
else
  note_fail "模型目录不存在：$MODELS"
fi

# 产线配置：一条 yaml = 一条可选产线（外加内置 OCR）
PIPE_DIR="$PROJECT_ROOT/config/pipelines"
if [ -d "$PIPE_DIR" ]; then
  PIPE_LIST="$(find "$PIPE_DIR" -maxdepth 1 -name '*.yaml' -exec basename {} .yaml \; 2>/dev/null | sort | tr '\n' ' ')"
  PIPE_N="$(find "$PIPE_DIR" -maxdepth 1 -name '*.yaml' | wc -l | tr -d ' ')"
  if [ "${PIPE_N:-0}" -gt 0 ]; then
    ok "产线配置 ${PIPE_N} 条：${PIPE_LIST}+ OCR（内置）"
  else
    warn "config/pipelines 下没有 yaml，将只有内置 OCR 产线可用"
  fi
  # 编码体检：非 UTF-8 的 yaml（GBK 另存 / macOS 的 ._*.yaml）在旧版本上会让服务启动即崩
  if [ -n "${PY:-}" ]; then
    BAD_CFG="$("$PY" - "$PIPE_DIR" <<'PY' 2>/dev/null || true
import sys
from pathlib import Path

for p in sorted(Path(sys.argv[1]).glob("*.yaml")):
    raw = p.read_bytes()
    if p.name.startswith(".") or raw[:4] == b"\x00\x05\x16\x07" or b"\x00" in raw[:2048]:
        print(f"{p.name}: 二进制/元数据文件（会被跳过）")
        continue
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        print(f"{p.name}: 非 UTF-8（{exc}）")
PY
)"
    if [ -n "$BAD_CFG" ]; then
      warn "产线配置里有非 UTF-8/元数据文件（运行时会跳过；1.0.0 老包会直接启动失败）："
      printf '%s\n' "$BAD_CFG" | sed 's/^/       /'
      warn "  建议删除 ._* 文件，或把其余文件另存为 UTF-8"
    else
      ok "产线配置编码正常（全部 UTF-8）"
    fi
  fi
else
  warn "缺少 config/pipelines 目录，将只有内置 OCR 产线可用"
fi

step "5/8 目录权限"
for d in "$PROJECT_ROOT/data" "$PROJECT_ROOT/logs"; do
  mkdir -p "$d" 2>/dev/null || true
  if [ -w "$d" ]; then ok "可写：$d"; else note_fail "不可写：$d"; fi
done

step "6/8 资源"
if [ -r /proc/meminfo ]; then
  MEM_MB="$(awk '/MemAvailable/{printf "%d", $2/1024}' /proc/meminfo)"
  info "可用内存: ${MEM_MB} MB"
  [ "${MEM_MB:-0}" -lt 1500 ] && warn "可用内存低于 1.5GB，PP-StructureV3 可能 OOM，建议调小 ocr.max_side_px / pdf_dpi"
  CPU_N="$(nproc 2>/dev/null || echo '?')"
  info "CPU 核心: $CPU_N"
fi
DISK_AVAIL="$(df -Pk "$PROJECT_ROOT" | awk 'NR==2{printf "%d", $4/1024}')"
info "项目所在分区可用: ${DISK_AVAIL} MB"
[ "${DISK_AVAIL:-0}" -lt 2000 ] && warn "磁盘可用空间不足 2GB，识别中间文件可能写入失败"

step "7/8 端口与离线策略"
PORT="$(config_get server.port 8000)"
if command -v ss >/dev/null 2>&1; then
  SS_OUT="$(ss -ltn 2>/dev/null || true)"
  if grep -q ":$PORT " <<< "$SS_OUT"; then
    warn "端口 $PORT 已被占用"
  else
    ok "端口 $PORT 空闲"
  fi
elif command -v lsof >/dev/null 2>&1; then
  lsof -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1 && warn "端口 $PORT 已被占用" || ok "端口 $PORT 空闲"
fi
info "运行期网络策略：模型取自 ${MODELS}，进程内不访问外网"

echo
if [ "$FAIL" -eq 0 ]; then
  ok "自检通过，可以启动服务：./scripts/start.sh"
else
  warn "自检发现 $FAIL 个问题，请按上面提示修复后重试"
  exit 1
fi
