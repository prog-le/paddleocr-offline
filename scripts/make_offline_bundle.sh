#!/usr/bin/env bash
# =============================================================================
# 生成"可直接拷贝到离线服务器"的整包（在联网机器上执行）
#
# 包内容 = 代码 + 前端 + 脚本 + 配置(含产线 yaml) + 模型 + 指定架构的 wheel
#          + 随包 CPython 3.12 运行时（目标机无需任何系统 python）
# 目标服务器解压后：./scripts/install_offline.sh && ./scripts/start.sh --daemon
#
# 用法：
#   ./scripts/make_offline_bundle.sh                 # 打当前架构（需先下载 wheel 与模型）
#   ./scripts/make_offline_bundle.sh --arch arm64
#   ./scripts/make_offline_bundle.sh --dry-run       # 只检查与打印计划，不落盘
#   ./scripts/make_offline_bundle.sh --with-docker   # 附带镜像 tar（已构建时）
#   ./scripts/make_offline_bundle.sh --skip-models   # 仅代码+依赖（模型另行拷贝）
#   ./scripts/make_offline_bundle.sh --skip-runtime  # 不打运行时（目标机已有 python3）
#   ./scripts/make_offline_bundle.sh --arch amd64 --device gpu
#                                                  # NVIDIA GPU 包（paddlepaddle-gpu，仅 amd64）
#   ./scripts/make_offline_bundle.sh --arch amd64 --device gpu \
#       --wheels-dir offline/wheels-gpu/amd64      # 与 CPU 轮子分目录存放时指定（推荐）
#
# 关于 GPU：**打包不需要显卡**。只要在任意 x86_64 Linux / macOS 上下载到 GPU 轮子即可；
# 真正的 GPU 推理验证在目标机（或临时租用的 GPU 云主机）上做。
# =============================================================================
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

ARCH="$(detect_arch)"
WITH_DOCKER=0
SKIP_MODELS=0
SKIP_RUNTIME=0
DRY_RUN=0
DEVICE="auto"          # auto | cpu | gpu
WHEELS_DIR_OPT=""      # 自定义 wheel 目录（默认 offline/wheels/$ARCH）
PY_MINOR=""            # wheel 的 cp 标签（如 cp312），后面探测
MODEL_COUNT=0          # --skip-models 时保持 0，避免 set -u 报未定义

while [ $# -gt 0 ]; do
  case "$1" in
    --arch) ARCH="$2"; shift 2 ;;
    --arch=*) ARCH="${1#*=}"; shift ;;
    --with-docker) WITH_DOCKER=1; shift ;;
    --skip-models) SKIP_MODELS=1; shift ;;
    --skip-runtime) SKIP_RUNTIME=1; shift ;;
    --device) DEVICE="$(echo "$2" | tr '[:upper:]' '[:lower:]')"; shift 2 ;;
    --device=*) DEVICE="$(echo "${1#*=}" | tr '[:upper:]' '[:lower:]')"; shift ;;
    --wheels-dir) WHEELS_DIR_OPT="$2"; shift 2 ;;
    --wheels-dir=*) WHEELS_DIR_OPT="${1#*=}"; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) die "未知参数：$1" ;;
  esac
done

VERSION="$(cd "$PROJECT_ROOT" && python3 -c 'import re,pathlib;m=re.search(r"__version__\s*=\s*\"([^\"]+)\"", pathlib.Path("backend/__init__.py").read_text());print(m.group(1) if m else "1.0.0")')"

case "$DEVICE" in
  cpu|gpu) ;;
  auto)
    # 默认按 wheel 目录里的 Paddle 变体判断（download_wheels 保证二者只存其一）
    _DETECT_DIR="${WHEELS_DIR_OPT:-$PROJECT_ROOT/offline/wheels/$ARCH}"
    case "$_DETECT_DIR" in /*) ;; *) _DETECT_DIR="$PROJECT_ROOT/$_DETECT_DIR" ;; esac
    if ls "$_DETECT_DIR"/paddlepaddle_gpu-*.whl >/dev/null 2>&1; then
      DEVICE="gpu"
      warn "--device 未指定，但 $_DETECT_DIR 里是 GPU 轮子 → 按 GPU 包打包"
      warn "  （要打 CPU 包请用 --device cpu，并先把 CPU 轮子放回该目录）"
    else
      DEVICE="cpu"
    fi
    ;;
  *) die "--device 只能是 auto / cpu / gpu" ;;
esac

if [ "$DEVICE" = "gpu" ]; then
  [ "$ARCH" = "amd64" ] || die "GPU 包仅支持 amd64（paddlepaddle-gpu 没有 aarch64 轮子）"
  NAME="paddlex-ocr-web-${VERSION}-${ARCH}-gpu"
else
  NAME="paddlex-ocr-web-${VERSION}-${ARCH}"
fi
DIST="$PROJECT_ROOT/dist"
STAGE="$DIST/$NAME"
TARBALL="$DIST/$NAME.tar.gz"

# ---------------------------------------------------------------- 垃圾清单
# 一份清单，三处共用（复制时排除 / 复制后清理 / 打包时 --exclude + 终检），
# 避免"某个环节漏了一种"就又有垃圾混进交付包。
# 分类：
#   macOS 元数据/资源分叉：.DS_Store、._*（AppleDouble）、.AppleDouble、__MACOSX、
#       .Spotlight-V100、.fseventsd、.TemporaryItems、.Trashes、.apdisk、
#       .VolumeIcon.icns、Icon\r、.com.apple.*  —— 这些都不是项目内容
#   Python 缓存：__pycache__、*.pyc、*.pyo、.pytest_cache、.mypy_cache、.ruff_cache
#   编辑器/系统杂项：*.swp、*~、Thumbs.db、desktop.ini、.ipynb_checkpoints
#   运行期产物：*.log、PaddleX 下载锁 locks/
#   模型下载器记账文件：.msc、.mv、._tmp（ModelScope 的缓存标记，推理不需要）
#   版本库占位：.gitkeep、.gitattributes
#   打包脚本临时文件：.requirements-*.txt
JUNK_NAMES=(
  '.DS_Store' '._*' '__MACOSX' '.AppleDouble' '.Spotlight-V100' '.fseventsd'
  '.TemporaryItems' '.Trashes' '.apdisk' '.VolumeIcon.icns' 'Icon?' '.com.apple.*'
  '__pycache__' '*.pyc' '*.pyo' '.pytest_cache' '.mypy_cache' '.ruff_cache'
  '.ipynb_checkpoints' '*.swp' '*~' 'Thumbs.db' 'desktop.ini'
  '*.log' 'locks'
  '.msc' '.mv' '._tmp'
  '.gitkeep' '.gitattributes'
  '.requirements-*.txt'
)
# 终检用的正则（与上面的 glob 一一对应；新增 glob 时记得同步这里）
JUNK_RE='/\.DS_Store$|/\._[^/]*$|/__MACOSX(/|$)|/\.AppleDouble/|/\.Spotlight-V100/|/\.fseventsd/|/\.TemporaryItems/|/\.Trashes/|/\.apdisk$|/\.VolumeIcon\.icns$|/Icon\r$|/\.com\.apple\.[^/]*$|/__pycache__/|\.py[co]$|/\.pytest_cache/|/\.mypy_cache/|/\.ruff_cache/|/\.ipynb_checkpoints/|\.swp$|~$|/Thumbs\.db$|/desktop\.ini$|\.log$|/locks/|/\.msc$|/\.mv$|/\._tmp(/|$)|/\.gitkeep$|/\.gitattributes$|/\.requirements-[^/]*\.txt$'

# 复制时排除（避免把 6 GB 垃圾搬来搬去再删）
FIND_EXCL=()
for _pat in "${JUNK_NAMES[@]}"; do FIND_EXCL+=( ! -name "$_pat" ); done
RSYNC_EXCL=()
for _pat in "${JUNK_NAMES[@]}"; do RSYNC_EXCL+=( --exclude="$_pat" ); done
TAR_EXCL=()
for _pat in "${JUNK_NAMES[@]}"; do TAR_EXCL+=( --exclude="$_pat" ); done
PRUNE_EXPR=()
for _pat in "${JUNK_NAMES[@]}"; do
  [ ${#PRUNE_EXPR[@]} -gt 0 ] && PRUNE_EXPR+=( -o )
  PRUNE_EXPR+=( -name "$_pat" )
done

print_banner "打包离线整包"
info "版本: $VERSION  架构: $ARCH  设备: $DEVICE"
info "输出: $TARBALL"
[ "$DRY_RUN" -eq 1 ] && warn "DRY-RUN 模式：只检查，不生成文件"

# ---------------------------------------------------------------- 1) 输入校验
step "1/5 校验输入物料"

if [ -n "$WHEELS_DIR_OPT" ]; then
  WHEELS="$WHEELS_DIR_OPT"
  case "$WHEELS" in /*) ;; *) WHEELS="$PROJECT_ROOT/$WHEELS" ;; esac
else
  WHEELS="$PROJECT_ROOT/offline/wheels/$ARCH"
fi
[ -d "$WHEELS" ] || die "缺少 $WHEELS
     请先在有网机器执行：./scripts/download_wheels.sh --arch $ARCH"
WHEEL_COUNT="$(find "$WHEELS" -name '*.whl' | wc -l | tr -d ' ')"
[ "$WHEEL_COUNT" -gt 0 ] || die "$WHEELS 中没有 wheel
     请在有网机器执行：./scripts/download_wheels.sh --arch $ARCH --python 3.12
     （--python 必须与目标服务器的 Python 版本一致）"
if [ "$DEVICE" = "gpu" ]; then
  ls "$WHEELS"/paddlepaddle_gpu-*.whl >/dev/null 2>&1 || die "$WHEELS 缺少 paddlepaddle-gpu wheel
     请先执行：./scripts/download_wheels.sh --arch amd64 --device gpu --cuda cu129
     （下载不需要显卡，任意 x86_64 机器/云主机都可以；轮子约 3.35 GB）"
  if ls "$WHEELS"/paddlepaddle-[0-9]*.whl >/dev/null 2>&1; then
    die "$WHEELS 同时存在 CPU 版与 GPU 版 Paddle 轮子（会装错）。
     请重新执行：./scripts/download_wheels.sh --arch amd64 --device gpu --cuda cu129
     （脚本会自动清掉 CPU 版残留）"
  fi
else
  ls "$WHEELS"/paddlepaddle-*.whl >/dev/null 2>&1 || die "$WHEELS 缺少 paddlepaddle wheel"
fi
ls "$WHEELS"/paddlex-*.whl >/dev/null 2>&1 || die "$WHEELS 缺少 paddlex wheel"
ok "wheel：$WHEEL_COUNT 个（$(du -sh "$WHEELS" | awk '{print $1}')）"

# 目标 Python 版本与 wheel 标签是否匹配（常见踩坑）
PY_MINOR="$(find "$WHEELS" -name 'paddlepaddle*.whl' -exec basename {} \; | head -1 | sed -E 's/.*-(cp[0-9]+)-.*/\1/')"
info "wheel 目标解释器标签：${PY_MINOR:-未知}（安装机需使用对应 Python 版本）"

# 随包 Python 运行时（目标机没有 python3 时必需）
RUNTIME_DIR="$PROJECT_ROOT/offline/python/$ARCH"
RUNTIME_PACK=""
RUNTIME_TAG=""
if [ "$SKIP_RUNTIME" -eq 0 ]; then
  if [ -d "$RUNTIME_DIR" ]; then
    RUNTIME_PACK="$(ls -1 "$RUNTIME_DIR"/*.tar.gz 2>/dev/null | head -1 || true)"
  fi
  if [ -z "$RUNTIME_PACK" ]; then
    die "缺少随包 Python 运行时：$RUNTIME_DIR/*.tar.gz
     请先在有网机器执行：./scripts/download_python_runtime.sh --arch $ARCH
     若目标机已有 python3 且版本与 wheel 一致，可加 --skip-runtime 跳过本检查"
  fi
  RUNTIME_TAG="$(basename "$RUNTIME_PACK" | sed -E 's/^cpython-([0-9.]+)\+.*/\1/')"
  [ -n "$RUNTIME_TAG" ] || RUNTIME_TAG="未知"
  ok "随包运行时：$(basename "$RUNTIME_PACK")（Python ${RUNTIME_TAG}，$(du -sh "$RUNTIME_PACK" | awk '{print $1}')）"
  # 运行时版本必须与 wheel 的 cp 标签一致
  if [ -n "$PY_MINOR" ] && [ "$PY_MINOR" != "未知" ]; then
    case "$RUNTIME_TAG" in
      "$(echo "$PY_MINOR" | sed -E 's/^cp([0-9])([0-9]+)$/\1.\2/')"*) : ;;
      *) warn "运行时是 Python ${RUNTIME_TAG}，而 wheel 标签是 $PY_MINOR —— 两者必须一致，否则离线安装会失败" ;;
    esac
  fi
else
  warn "--skip-runtime：不打运行时，目标机必须自带 python3（版本需与 wheel 标签一致）"
fi

MODELS="$(model_home)/official_models"
if [ "$SKIP_MODELS" -eq 0 ]; then
  if [ ! -d "$MODELS" ] || [ -z "$(ls -A "$MODELS" 2>/dev/null)" ]; then
    die "模型目录为空：$MODELS
     请先执行（不带参数即下载全部产线所需模型）：
       python3 scripts/download_models.py
     或按需逐条：
       python3 scripts/download_models.py --config config/pipelines/PP-StructureV3.yaml
       python3 scripts/download_models.py --config config/pipelines/PP-StructureV3-full.yaml
       python3 scripts/download_models.py --config config/pipelines/table_recognition_v2.yaml"
  fi
  MODEL_COUNT="$(find "$MODELS" -mindepth 1 -maxdepth 1 -type d | wc -l | tr -d ' ')"
  ok "模型：$MODEL_COUNT 个（$(du -sh "$MODELS" | awk '{print $1}')）"
  [ -f "$PROJECT_ROOT/models/manifest.json" ] || warn "缺少 models/manifest.json（可用 --manifest-only 重建）"
fi

for f in backend/main.py frontend/index.html config/config.yaml \
         config/pipelines/PP-StructureV3.yaml \
         config/pipelines/PP-StructureV3-full.yaml \
         config/pipelines/table_recognition_v2.yaml \
         docker/Dockerfile.gpu docker/docker-compose.gpu.yml \
         scripts/install_offline.sh scripts/start.sh requirements.txt \
         scripts/download_python_runtime.sh; do
  [ -e "$PROJECT_ROOT/$f" ] || die "缺少必要文件：$f"
done
ok "代码/配置/脚本齐全"

# ---------------------------------------------------------------- 垃圾体检
# 把"会被排除的东西"明确列出来：既能让用户确认垃圾确实被挡住了，
# 也便于发现新的垃圾类型（发现新类型就往脚本顶部的 JUNK_NAMES 里加一条）。
JUNK_SRC_LIST="$(
  {
    find "$PROJECT_ROOT/backend" "$PROJECT_ROOT/frontend" "$PROJECT_ROOT/config" \
         "$PROJECT_ROOT/scripts" "$PROJECT_ROOT/docker" "$PROJECT_ROOT/deploy" \
         "$PROJECT_ROOT/tests" \( "${PRUNE_EXPR[@]}" \) -print 2>/dev/null || true
    if [ "$SKIP_MODELS" -eq 0 ] && [ -d "$MODELS" ]; then
      find "$MODELS" \( "${PRUNE_EXPR[@]}" \) -print 2>/dev/null || true
    fi
    if [ "$SKIP_RUNTIME" -eq 0 ] && [ -d "$RUNTIME_DIR" ]; then
      find "$RUNTIME_DIR" \( "${PRUNE_EXPR[@]}" \) -print 2>/dev/null || true
    fi
    find "$WHEELS" \( "${PRUNE_EXPR[@]}" \) -print 2>/dev/null || true
  } | sed "s|$PROJECT_ROOT/||" | sort
)"
JUNK_SRC_COUNT="$(printf '%s\n' "$JUNK_SRC_LIST" | grep -c . || true)"
if [ "${JUNK_SRC_COUNT:-0}" -gt 0 ]; then
  info "垃圾体检：源目录中有 $JUNK_SRC_COUNT 个无关文件，打包时会全部排除"
  printf '%s\n' "$JUNK_SRC_LIST" | head -15 | sed 's/^/       - /'
  [ "$JUNK_SRC_COUNT" -gt 15 ] && info "       ……（共 $JUNK_SRC_COUNT 个，均不会进包）"
else
  ok "垃圾体检：源目录干净（无 macOS 元数据 / 字节码缓存 / 下载器记账文件）"
fi

if [ "$DRY_RUN" -eq 1 ]; then
  step "2/5 DRY-RUN 计划"
  cat <<EOF
  将打包到 $TARBALL ：
    backend/ frontend/ config/（含 pipelines/*.yaml）scripts/ docker/ deploy/ tests/
    requirements.txt requirements-macos.txt README.md
    $WHEELS/  （$WHEEL_COUNT 个 wheel，${PY_MINOR}）
$( [ "$SKIP_RUNTIME" -eq 0 ] && echo "    offline/python/$ARCH/  （$(basename "${RUNTIME_PACK:-}")，Python ${RUNTIME_TAG}）" )
$( [ "$SKIP_MODELS" -eq 0 ] && echo "    models/paddlex_home/official_models/（$MODEL_COUNT 个模型）" )
  排除垃圾文件（${#JUNK_NAMES[@]} 条规则，本次命中 ${JUNK_SRC_COUNT:-0} 个）：
        .DS_Store  ._*  __MACOSX  .AppleDouble  .Spotlight-V100  .fseventsd  .com.apple.*
        __pycache__  *.pyc  *.pyo  .pytest_cache  .mypy_cache  *.log  locks/
        .msc  .mv  ._tmp（模型下载器记账文件）  .gitkeep  .gitattributes  等
  排除目录：dist/  data/tasks/  .venv*  .runtime*
EOF
  ok "检查通过，未生成任何文件（去掉 --dry-run 即正式打包）"
  exit 0
fi

step "2/5 准备暂存目录"
rm -rf "$STAGE" 2>/dev/null || true
if [ -d "$STAGE" ]; then
  die "无法清理旧的暂存目录：$STAGE
     可能被占用或权限不足。请手动删除后重试：rm -rf \"$STAGE\""
fi
mkdir -p "$STAGE"

# ---------------------------------------------------------------- 2) 复制（带排除）
step "3/5 复制文件"

copy_tree() {
  local src="$1" dst="$2" before after
  mkdir -p "$dst"
  # 记录源文件数（已排除垃圾），复制后比对：cp 失败（权限/沙箱/磁盘满）绝不能静默产出半个包
  before="$(find "$src" -type f "${FIND_EXCL[@]}" 2>/dev/null | wc -l | tr -d ' ')"
  # 优先 rsync（保留权限、跨文件系统稳定、支持 --exclude）；没有 rsync 再退回 cp+清理
  if command -v rsync >/dev/null 2>&1; then
    rsync -a "${RSYNC_EXCL[@]}" "$src/" "$dst/" || die "复制失败：$src -> $dst"
  else
    cp -R "$src/." "$dst/" || die "复制失败：$src -> $dst"
  fi
  # 兜底清理：cp 分支、或 macOS 在复制过程中新生成的元数据文件
  find "$dst" \( "${PRUNE_EXPR[@]}" \) -prune -exec rm -rf {} + 2>/dev/null || true
  after="$(find "$dst" -type f 2>/dev/null | wc -l | tr -d ' ')"
  if [ "${after:-0}" -lt "${before:-0}" ]; then
    warn "复制不完整：$src 有 $before 个文件，目标只有 $after 个（可能被权限/沙箱拦截）"
    COPY_ERRORS=$((COPY_ERRORS + 1))
  fi
}

COPY_ERRORS=0
for d in backend frontend config scripts docker deploy tests; do
  copy_tree "$PROJECT_ROOT/$d" "$STAGE/$d"
done
cp "$PROJECT_ROOT/requirements.txt" "$PROJECT_ROOT/README.md" "$STAGE/"
[ -f "$PROJECT_ROOT/requirements-macos.txt" ] && cp "$PROJECT_ROOT/requirements-macos.txt" "$STAGE/"
[ -f "$PROJECT_ROOT/requirements-indra.txt" ] && cp "$PROJECT_ROOT/requirements-indra.txt" "$STAGE/"

mkdir -p "$STAGE/offline/wheels/$ARCH" "$STAGE/models/paddlex_home" "$STAGE/data" "$STAGE/logs"
copy_tree "$WHEELS" "$STAGE/offline/wheels/$ARCH"
# wheel 里可能有 3.35 GB 的 paddlepaddle-gpu，复制必须复核：
# 磁盘满 / 中断 / 沙箱拦截都不能静默产出"少几个包"的交付物。
STAGED_WHEELS="$(find "$STAGE/offline/wheels/$ARCH" -name '*.whl' | wc -l | tr -d ' ')"
[ "$STAGED_WHEELS" -eq "$WHEEL_COUNT" ] \
  || die "wheel 复制不完整：源 $WHEEL_COUNT 个，暂存目录只有 $STAGED_WHEELS 个"
SRC_KB="$(du -sk "$WHEELS" | awk '{print $1}')"
DST_KB="$(du -sk "$STAGE/offline/wheels/$ARCH" | awk '{print $1}')"
# 允许 1% 误差（块大小/文件系统对齐差异），明显偏小即判定为复制失败
[ "$DST_KB" -ge "$((SRC_KB - SRC_KB / 100))" ] \
  || die "wheel 复制体积异常：源 ${SRC_KB}KB，暂存 ${DST_KB}KB（可能磁盘满或被中断）"
ok "已复制 wheel：$STAGED_WHEELS 个（$(du -sh "$STAGE/offline/wheels/$ARCH" | awk '{print $1}')）"

# 随包 Python 运行时（只放目标架构那一份，避免把另一架构也带进去）
if [ "$SKIP_RUNTIME" -eq 0 ]; then
  mkdir -p "$STAGE/offline/python/$ARCH"
  cp -R "$RUNTIME_DIR/." "$STAGE/offline/python/$ARCH/"
  ok "已复制随包运行时：$(basename "$RUNTIME_PACK")"
else
  rm -rf "$STAGE/offline/python"
fi
if [ "$SKIP_MODELS" -eq 0 ]; then
  copy_tree "$MODELS" "$STAGE/models/paddlex_home/official_models"
  [ -f "$PROJECT_ROOT/models/manifest.json" ] && cp "$PROJECT_ROOT/models/manifest.json" "$STAGE/models/"
  [ -f "$PROJECT_ROOT/models/README.md" ] && cp "$PROJECT_ROOT/models/README.md" "$STAGE/models/"
  # 可选 Indra OCR 权重（models/indra）；未下载时只有 .gitkeep，不复制
  if [ -d "$PROJECT_ROOT/models/indra" ] && \
     [ -n "$(find "$PROJECT_ROOT/models/indra" -type f ! -name '.gitkeep' -print -quit 2>/dev/null)" ]; then
    copy_tree "$PROJECT_ROOT/models/indra" "$STAGE/models/indra"
  fi
fi
if [ "${COPY_ERRORS:-0}" -gt 0 ]; then
  die "有 $COPY_ERRORS 个目录复制不完整，已中止（避免产出残缺的交付包）"
fi

# data/ logs/ 靠 mkdir 建空目录即可（tar 会保留空目录条目），
# 不再放 .gitkeep —— 那是版本库占位文件，对交付包是垃圾。
chmod +x "$STAGE/scripts/"*.sh "$STAGE/docker/"*.sh 2>/dev/null || true

if [ "$WITH_DOCKER" -eq 1 ]; then
  IMG_TAR="$PROJECT_ROOT/dist/paddlex-ocr-web-${VERSION}-${ARCH}.image.tar.gz"
  if [ -f "$IMG_TAR" ]; then
    cp "$IMG_TAR" "$STAGE/"
    ok "已包含 $(basename "$IMG_TAR")"
  else
    warn "未找到 ${IMG_TAR}，跳过（可先 docker/build_image.sh 再 docker/export_image.sh）"
  fi
fi

GPU_NOTE=""
if [ "$DEVICE" = "gpu" ]; then
  GPU_NOTE='
GPU 版额外要求（目标机）：
  * NVIDIA 驱动 >= 575.57.08（cu129），nvidia-smi 可用；无卡机器上 import paddle 会报
    libcuda 缺失，属正常（安装脚本会降级为 WARN，CUDA 验收请在带卡机器做）；
  * glibc >= 2.28（wheel 含 manylinux_2_28 包）；
  * 磁盘：装完约 7 GB（.venv）+ 模型 2.3 GB。
'
fi

cat > "$STAGE/DEPLOY_QUICKSTART.txt" <<EOF
=============================================================
 PaddleX OCR Web ${VERSION} · ${ARCH} · ${DEVICE} 离线部署（3 步）
=============================================================
# 1. 解压
tar xzf $(basename "$TARBALL")
cd ${NAME}

# 2. 安装依赖（完全离线，不联网）
./scripts/install_offline.sh$([ "$DEVICE" = "gpu" ] && echo " --device gpu")

# 3. 启动
./scripts/start.sh --daemon

访问      : http://<服务器IP>:$(config_get server.port 8000)
管理后台  : http://<服务器IP>:$(config_get server.port 8000)/admin
默认账号  : 见 config/config.yaml（请立即修改 server.admin_password）
日志      : logs/app.log 、 logs/server.log
数据/结果 : data/tasks/

注意：wheel 的目标 Python 为 ${PY_MINOR:-?}；包内已带 CPython ${RUNTIME_TAG} 运行时，
      目标机无需安装 Python，./scripts/install_offline.sh 会自动解包并使用它。
${GPU_NOTE}$([ "$SKIP_MODELS" -eq 1 ] && printf '\n模型未随包提供：把 models-paddlex_home-*.tar.gz 放到包根目录后执行\n  tar xzf models-paddlex_home-*.tar.gz\n（或在联网机器下载后用 scp/rsync 拷贝 models/ 目录）')
详细说明见 README.md「本地离线部署步骤」。
=============================================================
EOF

# ---------------------------------------------------------------- 3) 打包
step "4/5 生成压缩包"
mkdir -p "$DIST"
# macOS 的 bsdtar 默认会把扩展属性（com.apple.provenance 等）写进 pax 记录，
# 这些记录在 Linux/Windows 的第三方解压工具里常被摊成 ._xxx / __MACOSX 垃圾文件。
# COPYFILE_DISABLE=1 关掉 AppleDouble，--no-xattrs 关掉 xattr 记录（不支持则自动跳过）。
# --exclude：即便前面漏了，tar 这一层再挡一次（垃圾清单与 copy_tree 共用）。
TAR_XATTR_OPT=()
if tar -cf /dev/null --no-xattrs --files-from /dev/null >/dev/null 2>&1; then
  TAR_XATTR_OPT=( --no-xattrs )
else
  info "当前 tar 不支持 --no-xattrs，跳过（不影响包内容）"
fi
COPYFILE_DISABLE=1 tar czf "$TARBALL" -C "$DIST" "${TAR_XATTR_OPT[@]}" "${TAR_EXCL[@]}" "$NAME"
SIZE="$(du -h "$TARBALL" | awk '{print $1}')"
ok "已生成 ${TARBALL}（${SIZE}）"

# ---------------------------------------------------------------- 4) 自检
step "5/5 包内容自检"
# 只列一次目录树。GPU 包约 8.7 GB，反复 tar tzf 会把构建时间拖到十几分钟，
# 且 `tar tzf | grep -q` 在 pipefail 下会因 SIGPIPE 误报"缺少文件"。
LISTING="$(mktemp "${TMPDIR:-/tmp}/pdx-pkg-list.XXXXXX")"
trap 'rm -f "$LISTING"' EXIT
tar tzf "$TARBALL" > "$LISTING" || die "无法读取刚生成的 $TARBALL"

MISSING=0
need_file() { grep -qxF "$1" "$LISTING" || { warn "包内缺少 $1"; MISSING=$((MISSING+1)); }; }
need_dir()  { grep -qF "$1" "$LISTING" || { warn "包内缺少 $1"; MISSING=$((MISSING+1)); }; }

for path in "$NAME/backend/main.py" "$NAME/frontend/index.html" "$NAME/config/config.yaml" \
            "$NAME/config/pipelines/PP-StructureV3.yaml" \
            "$NAME/config/pipelines/PP-StructureV3-full.yaml" \
            "$NAME/config/pipelines/table_recognition_v2.yaml" \
            "$NAME/scripts/install_offline.sh" "$NAME/requirements.txt"; do
  need_file "$path"
done
if [ "$SKIP_MODELS" -eq 0 ]; then
  need_dir "$NAME/models/paddlex_home/official_models/"
fi
need_dir "$NAME/offline/wheels/$ARCH/"
if [ "$DEVICE" = "gpu" ]; then
  grep -qE "/$ARCH/paddlepaddle_gpu-[^/]*\.whl$" "$LISTING" \
    || { warn "包内缺少 paddlepaddle-gpu wheel"; MISSING=$((MISSING+1)); }
else
  grep -qE "/$ARCH/paddlepaddle-[0-9][^/]*\.whl$" "$LISTING" \
    || { warn "包内缺少 paddlepaddle wheel"; MISSING=$((MISSING+1)); }
fi
grep -qE "/$ARCH/paddlex-[^/]*\.whl$" "$LISTING" \
  || { warn "包内缺少 paddlex wheel"; MISSING=$((MISSING+1)); }
if [ "$SKIP_RUNTIME" -eq 0 ]; then
  need_dir "$NAME/offline/python/$ARCH/"
fi
# 包内 wheel 个数必须与源目录一致（tar 阶段也可能因磁盘满截断）
PKG_WHEELS="$(grep -cE "^$NAME/offline/wheels/$ARCH/.*\.whl$" "$LISTING" || true)"
if [ "${PKG_WHEELS:-0}" -ne "$WHEEL_COUNT" ]; then
  warn "包内 wheel 个数 ${PKG_WHEELS:-0} ≠ 源目录 ${WHEEL_COUNT}"
  MISSING=$((MISSING+1))
fi
if [ "$MISSING" -eq 0 ]; then
  # 垃圾文件终检：命中任意一条即判失败，并打印具体路径（不再只报一个数字）
  JUNK_HITS="$(grep -aE "$JUNK_RE" "$LISTING" | head -20 || true)"
  JUNK_COUNT="$(grep -acE "$JUNK_RE" "$LISTING" || true)"
  if [ "${JUNK_COUNT:-0}" -gt 0 ]; then
    warn "包内有 ${JUNK_COUNT} 个垃圾/无关文件（缓存、macOS 元数据、模型下载器记账文件等）："
    printf '%s\n' "$JUNK_HITS" | sed -E 's/^/       /' >&2
    [ "$JUNK_COUNT" -gt 20 ] && warn "      ……（仅显示前 20 条）"
    die "请检查 JUNK_NAMES 清单（脚本顶部）后重打"
  fi
  ok "必需文件齐全（wheel ${PKG_WHEELS} 个），且无缓存/元数据垃圾"
else
  die "包内缺少 $MISSING 项，请检查上面的告警"
fi

echo "  内容概览："
echo "    wheel  : $(find "$STAGE/offline/wheels/$ARCH" -name '*.whl' | wc -l | tr -d ' ') 个（${PY_MINOR}）
$( [ "$SKIP_RUNTIME" -eq 0 ] && echo "    运行时 : $(du -sh "$STAGE/offline/python/$ARCH" | awk '{print $1}')（Python ${RUNTIME_TAG}，目标机无需系统 python）" )"
[ "$SKIP_MODELS" -eq 0 ] && echo "    模型   : $(find "$STAGE/models/paddlex_home/official_models" -mindepth 1 -maxdepth 1 -type d | wc -l | tr -d ' ') 个，$(du -sh "$STAGE/models" | awk '{print $1}')"
echo "    设备   : $DEVICE$([ "$DEVICE" = "gpu" ] && echo "（GPU 版 Paddle，容器请叠加 docker-compose.gpu.yml）")"
echo "    压缩包 : $SIZE"

echo
echo "拷贝到离线服务器后执行："
echo "  tar xzf $(basename "$TARBALL") && cd $NAME && ./scripts/install_offline.sh$([ "$DEVICE" = "gpu" ] && echo " --device gpu") && ./scripts/start.sh --daemon"
echo "（正式上线前记得改 config/config.yaml 里的 server.admin_password）"
