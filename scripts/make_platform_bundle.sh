#!/usr/bin/env bash
# =============================================================================
# 生成 macOS / Windows 离线整包（Linux 用 make_offline_bundle.sh）
#
# 包内容 = 代码 + 前端 + 脚本 + 配置 + 模型 + 目标平台 wheel + 随包 CPython 3.12
#   macOS   -> paddlex-ocr-web-<ver>-macos-arm64.tar.gz（解压后 ./scripts/install_offline.sh）
#   Windows -> paddlex-ocr-web-<ver>-windows-amd64.zip（解压后 scripts\install_windows.ps1）
#
# 用法：
#   ./scripts/make_platform_bundle.sh --os macos --arch arm64
#   ./scripts/make_platform_bundle.sh --os windows --arch amd64
#   ./scripts/make_platform_bundle.sh --os windows --arch amd64 --skip-models
#   ./scripts/make_platform_bundle.sh --os macos --arch arm64 --skip-runtime
#   ./scripts/make_platform_bundle.sh --os windows --arch amd64 --dry-run
#
# 上游限制（脚本会拒绝而不是打出坏包）：
#   * macos-amd64：PyPI 没有 paddlepaddle macOS x86_64 轮子
#   * windows-arm64：PyPI 没有 paddlepaddle Windows ARM64 轮子
#   * 两个平台都只有 CPU 版（GPU 仅 linux-amd64）
# =============================================================================
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

TARGET_OS=""
ARCH=""
SKIP_MODELS=0
SKIP_RUNTIME=0
DRY_RUN=0

while [ $# -gt 0 ]; do
  case "$1" in
    --os) TARGET_OS="$2"; shift 2 ;;
    --os=*) TARGET_OS="${1#*=}"; shift ;;
    --arch) ARCH="$2"; shift 2 ;;
    --arch=*) ARCH="${1#*=}"; shift ;;
    --skip-models) SKIP_MODELS=1; shift ;;
    --skip-runtime) SKIP_RUNTIME=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) die "未知参数：$1" ;;
  esac
done

case "$TARGET_OS" in
  macos)   [ -n "$ARCH" ] || ARCH="arm64" ;;
  windows) [ -n "$ARCH" ] || ARCH="amd64" ;;
  *) die "必须指定 --os macos 或 --os windows（Linux 请用 make_offline_bundle.sh）" ;;
esac
TARGET="${TARGET_OS}-${ARCH}"
target_supported "$TARGET" || die "$(target_support_note "$TARGET")"

VERSION="$(cd "$PROJECT_ROOT" && python3 -c 'import re,pathlib;m=re.search(r"__version__\s*=\s*\"([^\"]+)\"", pathlib.Path("backend/__init__.py").read_text());print(m.group(1) if m else "1.0.0")')"
NAME="paddlex-ocr-web-${VERSION}-${TARGET}"
DIST="$PROJECT_ROOT/dist"
STAGE="$DIST/$NAME"
if [ "$TARGET_OS" = "windows" ]; then
  ARCHIVE="$DIST/$NAME.zip"
else
  ARCHIVE="$DIST/$NAME.tar.gz"
fi

WHEELS="$(target_wheels_dir "$TARGET")"
RUNTIME_DIR="$(target_runtime_dir "$TARGET")"
MODEL_HOME="$(model_home)"
MODELS="$MODEL_HOME/official_models"

print_banner "打包 ${TARGET} 离线整包"
info "版本: $VERSION"
info "wheel: $WHEELS"
info "运行时: $RUNTIME_DIR"
info "模型: $MODELS"

# ---------------------------------------------------------------- 前置校验
step "1/6 校验依赖与模型"

[ -d "$WHEELS" ] || die "缺少 $WHEELS：先执行 ./scripts/download_wheels.sh --os ${TARGET_OS} --arch ${ARCH}"
WHEEL_COUNT="$(find "$WHEELS" -name '*.whl' | wc -l | tr -d ' ')"
[ "$WHEEL_COUNT" -gt 0 ] || die "$WHEELS 中没有 wheel"
case "$TARGET_OS" in
  macos)
    ls "$WHEELS"/paddlepaddle-*macosx*arm64*.whl >/dev/null 2>&1 \
      || die "$WHEELS 中缺少 macOS arm64 的 paddlepaddle wheel"
    ;;
  windows)
    ls "$WHEELS"/paddlepaddle-*win_amd64*.whl >/dev/null 2>&1 \
      || die "$WHEELS 中缺少 Windows amd64 的 paddlepaddle wheel"
    ;;
esac
ls "$WHEELS"/paddlex-*.whl >/dev/null 2>&1 || die "$WHEELS 中缺少 paddlex wheel"
ok "wheel $WHEEL_COUNT 个"

RT_PACK="$(runtime_tarball "$TARGET" || true)"
if [ "$SKIP_RUNTIME" -eq 0 ]; then
  if [ -z "$RT_PACK" ] || [ ! -f "$RT_PACK" ]; then
    if [ "$TARGET_OS" = "macos" ]; then
      die "缺少 macOS 运行时：./scripts/download_python_runtime.sh --os macos --arch arm64"
    else
      die "缺少 Windows 运行时：./scripts/download_python_runtime.sh --os windows --arch amd64"
    fi
  fi
  ok "运行时: $(basename "$RT_PACK")"
else
  warn "--skip-runtime：目标机必须自带 Python 3.10-3.13"
fi

MODEL_COUNT=0
if [ "$SKIP_MODELS" -eq 0 ]; then
  [ -d "$MODELS" ] || die "模型目录不存在：$MODELS（先在联网机器执行 python3 scripts/download_models.py）"
  MODEL_COUNT="$(find "$MODELS" -mindepth 1 -maxdepth 1 -type d | wc -l | tr -d ' ')"
  [ "$MODEL_COUNT" -gt 0 ] || die "$MODELS 为空：先在联网机器执行 python3 scripts/download_models.py"
  ok "模型产线 $MODEL_COUNT 条"
else
  warn "--skip-models：包内不含权重，安装后需手动放入 models/paddlex_home/official_models/"
fi

if [ "$DRY_RUN" -eq 1 ]; then
  step "dry-run"
  info "将生成：$ARCHIVE"
  info "目标平台：$TARGET（CPU）"
  exit 0
fi

# ---------------------------------------------------------------- 清理与复制
step "2/6 准备暂存目录"
rm -rf "$STAGE"
mkdir -p "$STAGE"

RSYNC_EXCLUDES=(
  --exclude=.git --exclude=.venv --exclude=.runtime --exclude=.runtime_env
  --exclude=__pycache__ --exclude='*.py[co]' --exclude=.pytest_cache
  --exclude=.mypy_cache --exclude=.ruff_cache --exclude=.idea --exclude=.vscode
  --exclude=.env --exclude=.DS_Store --exclude='._*' --exclude=.AppleDouble
  --exclude=.Spotlight-V100 --exclude=.fseventsd --exclude=.TemporaryItems
  --exclude=data --exclude=logs --exclude=dist --exclude=offline --exclude=models
  --exclude='*.log' --exclude='*.pid' --exclude='*.tar.gz' --exclude='*.zip'
)

copy_path() {  # $1=相对路径（文件或目录）
  local rel="$1"
  [ -e "$PROJECT_ROOT/$rel" ] || return 0
  if command -v rsync >/dev/null 2>&1; then
    rsync -a "${RSYNC_EXCLUDES[@]}" "$PROJECT_ROOT/$rel" "$STAGE/"
  else
    cp -a "$PROJECT_ROOT/$rel" "$STAGE/"
  fi
}

step "3/6 复制代码与配置"
for rel in backend frontend config scripts deploy tests; do
  copy_path "$rel"
done
for f in README.md requirements.txt requirements-macos.txt requirements-windows.txt requirements-indra.txt; do
  [ -f "$PROJECT_ROOT/$f" ] && cp -a "$PROJECT_ROOT/$f" "$STAGE/"
done
if [ -d "$PROJECT_ROOT/docs" ]; then copy_path docs; fi
find "$STAGE" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$STAGE" -name '*.py[co]' -delete 2>/dev/null || true
ok "代码复制完成"

step "4/6 放入 wheel / 运行时 / 模型"
mkdir -p "$STAGE/offline/wheels/$TARGET" "$STAGE/offline/python/$TARGET"
cp -a "$WHEELS"/. "$STAGE/offline/wheels/$TARGET/"
if [ "$SKIP_RUNTIME" -eq 0 ] && [ -n "$RT_PACK" ]; then
  cp -a "$RT_PACK" "$STAGE/offline/python/$TARGET/"
  [ -f "$RUNTIME_DIR/runtime-info.txt" ] && cp -a "$RUNTIME_DIR/runtime-info.txt" "$STAGE/offline/python/$TARGET/"
  [ -f "$RUNTIME_DIR/$(basename "$RT_PACK").sha256" ] && cp -a "$RUNTIME_DIR/$(basename "$RT_PACK").sha256" "$STAGE/offline/python/$TARGET/"
fi
if [ "$SKIP_MODELS" -eq 0 ]; then
  mkdir -p "$STAGE/models/paddlex_home"
  cp -a "$MODELS" "$STAGE/models/paddlex_home/"
  [ -f "$PROJECT_ROOT/models/manifest.json" ] && cp -a "$PROJECT_ROOT/models/manifest.json" "$STAGE/models/"
  [ -f "$PROJECT_ROOT/models/README.md" ] && cp -a "$PROJECT_ROOT/models/README.md" "$STAGE/models/"
  if [ -d "$PROJECT_ROOT/models/indra" ] && \
     [ -n "$(find "$PROJECT_ROOT/models/indra" -type f ! -name '.gitkeep' -print -quit 2>/dev/null)" ]; then
    cp -a "$PROJECT_ROOT/models/indra" "$STAGE/models/"
  fi
fi
chmod +x "$STAGE"/scripts/*.sh "$STAGE"/deploy/*.sh 2>/dev/null || true
ok "离线资产已放入"

# ---------------------------------------------------------------- 快速说明
step "5/6 生成平台说明"
if [ "$TARGET_OS" = "macos" ]; then
  cat > "$STAGE/DEPLOY_QUICKSTART.txt" <<EOF
PaddleX OCR Web · macOS Apple Silicon 离线包

支持：macOS 11+，Apple Silicon (arm64)，CPU 推理。
注意：Paddle 版本为 3.3.1（PyPI 没有 macOS 3.4.0 / Intel 轮子）。

安装：
  cd $NAME
  ./scripts/install_offline.sh
  ./scripts/start.sh              # 前台
  ./scripts/start.sh --daemon     # 后台

首次启动前如果模型未随包提供：
  tar xzf models-*.tar.gz         # 或在联网机器下载后拷贝 models/
EOF
else
  cat > "$STAGE/DEPLOY_QUICKSTART.txt" <<EOF
PaddleX OCR Web · Windows x64 离线包

支持：Windows 10 1803+ / 11 / Server 2016+，64 位，CPU 推理。
前置：Microsoft Visual C++ 2015-2022 Redistributable (x64)。
注意：Paddle 版本为 3.3.1（Windows 无 3.4.0 / ARM64 / GPU 轮子）。

安装（请先完整解压 zip，不要直接在压缩包内运行）：
  powershell -ExecutionPolicy Bypass -File scripts\\install_windows.ps1

启动：
  powershell -ExecutionPolicy Bypass -File scripts\\start_windows.ps1
  powershell -ExecutionPolicy Bypass -File scripts\\start_windows.ps1 -Daemon
停止：
  powershell -ExecutionPolicy Bypass -File scripts\\stop_windows.ps1

端口取 config\\config.yaml 的 server.port，需在防火墙放行。
$([ "$SKIP_MODELS" -eq 1 ] && printf '\n模型未随包提供：把 models-paddlex_home-*.tar.gz 放到包根目录后执行\r\n  tar.exe -xzf models-paddlex_home-*.tar.gz\r\n')
EOF
fi
ok "说明文件已生成"

# ---------------------------------------------------------------- 打包
step "6/6 压缩与自检"
case "$TARGET_OS" in
  macos)
    COPYFILE_DISABLE=1 tar czf "$ARCHIVE" -C "$DIST" \
      --exclude='.DS_Store' --exclude='._*' --exclude='.AppleDouble' \
      --exclude='__pycache__' --exclude='*.pyc' "$NAME"
    # 先落一份清单再 grep：`tar tzf | grep -q` 在 pipefail 下会因 SIGPIPE 误报缺文件
    LISTING="$(mktemp "${TMPDIR:-/tmp}/pdx-mac-list.XXXXXX")"
    tar tzf "$ARCHIVE" > "$LISTING" || { rm -f "$LISTING"; die "无法读取 $ARCHIVE"; }
    if ! grep -E "^${NAME}/(backend/main.py|frontend/index.html|scripts/install_offline.sh|offline/wheels/${TARGET}/)" \
         "$LISTING" >/dev/null; then
      rm -f "$LISTING"
      die "包内容自检失败：$ARCHIVE"
    fi
    rm -f "$LISTING"
    ;;
  windows)
    "$(command -v python3)" - "$STAGE" "$ARCHIVE" "$NAME" <<'PY'
import os, sys, zipfile
stage, archive, name = sys.argv[1:4]
skip_dirs = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
    for root, dirs, files in os.walk(stage):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for f in files:
            if f.endswith((".pyc", ".pyo", ".log", ".pid")):
                continue
            src = os.path.join(root, f)
            rel = os.path.relpath(src, stage).replace(os.sep, "/")
            z.write(src, f"{name}/{rel}")
PY
    "$(command -v python3)" - "$ARCHIVE" "$NAME" "$TARGET" <<'PY' || die "zip 自检失败"
import sys, zipfile
archive, name, target = sys.argv[1:4]
need = [f"{name}/backend/main.py", f"{name}/frontend/index.html",
        f"{name}/scripts/install_windows.ps1",
        f"{name}/requirements-windows.txt"]
with zipfile.ZipFile(archive) as z:
    names = set(z.namelist())
for n in need:
    if n not in names:
        sys.exit(f"缺少 {n}")
if not any(n.startswith(f"{name}/offline/wheels/{target}/") and n.endswith(".whl") for n in names):
    sys.exit("zip 中没有 wheel")
PY
    ;;
esac

SIZE="$(du -sh "$ARCHIVE" | awk '{print $1}')"
if command -v shasum >/dev/null 2>&1; then
  SHA="$(shasum -a 256 "$ARCHIVE" | awk '{print $1}')"
else
  SHA="$(sha256sum "$ARCHIVE" | awk '{print $1}')"
fi
echo "$SHA  $(basename "$ARCHIVE")" > "$ARCHIVE.sha256"

ok "完成：$ARCHIVE（$SIZE）"
info "sha256: $SHA"
info "包内 wheel: $(find "$STAGE/offline/wheels" -name '*.whl' | wc -l | tr -d ' ')  模型: $([ "$SKIP_MODELS" -eq 1 ] && echo '未包含' || echo '已包含')"
