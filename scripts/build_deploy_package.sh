#!/usr/bin/env bash
# =============================================================================
# 一键生成全平台离线部署包（在【有外网】的构建机执行）
#
# 默认：当前操作系统 + 当前架构
# 全平台：--all（linux-amd64/arm64 + macos-arm64 + windows-amd64，均为 CPU）
# 追加 GPU：--all --with-gpu（额外 linux-amd64-gpu）
#
# 用法：
#   ./scripts/build_deploy_package.sh                          # 当前平台
#   ./scripts/build_deploy_package.sh --all                    # 全部 CPU 平台
#   ./scripts/build_deploy_package.sh --all --with-gpu         # 再打 Linux GPU 包
#   ./scripts/build_deploy_package.sh --targets "linux-amd64,macos-arm64"
#   ./scripts/build_deploy_package.sh --all --models-separate  # 模型单独一个包，避免 4 份拷贝
#   ./scripts/build_deploy_package.sh --all --skip-download    # 复用已下载的 wheel/运行时
#   ./scripts/build_deploy_package.sh --all --dry-run          # 只打印计划
#   ./scripts/build_deploy_package.sh --list-targets           # 查看支持矩阵
#
# 平台限制（来自 Paddle/PaddleX 上游发布物，不是本项目的选择）：
#   * linux-arm64 只有 CPU；GPU wheel 仅 linux x86_64
#   * macos 只有 Apple Silicon + Paddle 3.3.1（Intel Mac 无轮子）
#   * windows 只有 x86_64 + Paddle 3.3.1（ARM64/GPU 无轮子）
#
# 产物：dist/release-<version>/
#   paddlex-ocr-web-<ver>-<target>.tar.gz|.zip
#   models-paddlex_home-<ver>.tar.gz      （--models-separate 时）
#   SHA256SUMS / release-manifest.json / DEPLOY_ALL.txt
# =============================================================================
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

TARGETS_ARG=""
USE_ALL=0
WITH_GPU=0
CUDA="${CUDA:-cu129}"
SKIP_DOWNLOAD=0
SKIP_RUNTIME=0
SKIP_MODELS=0
MODELS_SEPARATE=0
DRY_RUN=0
LIST_TARGETS=0

while [ $# -gt 0 ]; do
  case "$1" in
    --targets) TARGETS_ARG="$2"; shift 2 ;;
    --targets=*) TARGETS_ARG="${1#*=}"; shift ;;
    --all) USE_ALL=1; shift ;;
    --with-gpu) WITH_GPU=1; shift ;;
    --cuda) CUDA="$2"; shift 2 ;;
    --cuda=*) CUDA="${1#*=}"; shift ;;
    --skip-download) SKIP_DOWNLOAD=1; shift ;;
    --skip-runtime) SKIP_RUNTIME=1; shift ;;
    --skip-models) SKIP_MODELS=1; shift ;;
    --models-separate) MODELS_SEPARATE=1; SKIP_MODELS=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --list-targets) LIST_TARGETS=1; shift ;;
    -h|--help) sed -n '2,35p' "$0"; exit 0 ;;
    *) die "未知参数：$1" ;;
  esac
done

print_banner "一键生成离线部署包"

if [ "$LIST_TARGETS" -eq 1 ]; then
  echo "支持的部署目标："
  echo
  printf '  %-18s %-8s %-7s %-18s %s\n' "TARGET" "OS" "DEVICE" "PADDLE" "REQUIREMENTS"
  for t in "${SUPPORTED_TARGETS[@]}"; do
    case "$t" in
      linux-*)   pv="3.4.0" ;;
      macos-*)   pv="3.3.1" ;;
      windows-*) pv="3.3.1" ;;
    esac
    printf '  %-18s %-8s %-7s %-18s %s\n' "$t" "$(target_os "$t")" "$(target_device "$t")" "$pv" "$(target_requirements "$t")"
  done
  echo
  echo "不支持：macos-amd64（Paddle 无 macOS x86_64 轮子）、windows-arm64、linux-arm64 GPU。"
  echo "说明：linux-arm64 CPU 通过 Paddle 官方源获取；macOS/Windows 使用 PyPI 的 3.3.1。"
  exit 0
fi

# ---------------------------------------------------------------- 目标解析
if [ -n "$TARGETS_ARG" ]; then
  IFS=',' read -r -a RAW_TARGETS <<< "$TARGETS_ARG"
elif [ "$USE_ALL" -eq 1 ]; then
  RAW_TARGETS=("${CPU_TARGETS[@]}")
else
  RAW_TARGETS=("$(target_id)")
fi
[ ${#RAW_TARGETS[@]} -gt 0 ] || die "没有目标平台"

TARGET_LIST=()
for raw in "${RAW_TARGETS[@]}"; do
  t="${raw//[[:space:]]/}"
  [ -n "$t" ] || continue
  target_supported "$t" || die "$(target_support_note "$t")"
  duplicate=0
  for x in ${TARGET_LIST[@]+"${TARGET_LIST[@]}"}; do [ "$x" = "$t" ] && duplicate=1; done
  [ "$duplicate" -eq 0 ] && TARGET_LIST+=("$t")
done
if [ "$WITH_GPU" -eq 1 ]; then
  target_supported "linux-amd64-gpu" || die "GPU 目标不受支持"
  found=0
  for x in ${TARGET_LIST[@]+"${TARGET_LIST[@]}"}; do [ "$x" = "linux-amd64-gpu" ] && found=1; done
  [ "$found" -eq 1 ] || TARGET_LIST+=("linux-amd64-gpu")
fi
[ ${#TARGET_LIST[@]} -gt 0 ] || die "目标列表为空（$(target_support_note "$(target_id)")）"

VERSION="$(cd "$PROJECT_ROOT" && python3 -c 'import re,pathlib;m=re.search(r"__version__\s*=\s*\"([^\"]+)\"", pathlib.Path("backend/__init__.py").read_text());print(m.group(1) if m else "1.0.0")')"
RELEASE_DIR="$PROJECT_ROOT/dist/release-$VERSION"

step "构建计划（版本 $VERSION）"
for t in "${TARGET_LIST[@]}"; do
  printf '  %-18s os=%-7s arch=%-6s device=%-4s wheels=%s\n' \
    "$t" "$(target_os "$t")" "$(target_arch_id "$t")" "$(target_device "$t")" \
    "$(target_wheels_dir "$t" | sed "s|$PROJECT_ROOT/||")"
done
echo
info "模式：$([ "$SKIP_MODELS" -eq 1 ] && echo '模型不内嵌' || echo '模型内嵌')，$([ "$SKIP_DOWNLOAD" -eq 1 ] && echo '复用已有下载' || echo '自动下载依赖')"
[ "$MODELS_SEPARATE" -eq 1 ] && info "将额外生成 models-paddlex_home-${VERSION}.tar.gz（所有包共用）"

# ---------------------------------------------------------------- 构建前检查
step "检查构建环境"
PYTHON3="$(command -v python3 || true)"
[ -n "$PYTHON3" ] || die "需要 python3（读取版本、生成 zip 与清单）"
info "python3: $($PYTHON3 -c 'import sys;print(sys.version.split()[0])')"
if [ "$SKIP_DOWNLOAD" -eq 0 ]; then
  "$PYTHON3" -m pip --version >/dev/null 2>&1 || die "需要 pip：python3 -m pip --version 失败"
  command -v curl >/dev/null 2>&1 || die "需要 curl（下载随包 Python 运行时）"
fi
command -v tar >/dev/null 2>&1 || die "需要 tar"
command -v find >/dev/null 2>&1 || die "需要 find"
command -v du >/dev/null 2>&1 || die "需要 du"

MODEL_HOME="$(model_home)"
if [ "$SKIP_MODELS" -eq 0 ] || [ "$MODELS_SEPARATE" -eq 1 ]; then
  if [ ! -d "$MODEL_HOME/official_models" ] || [ -z "$(ls -A "$MODEL_HOME/official_models" 2>/dev/null)" ]; then
    die "模型目录为空：$MODEL_HOME/official_models
     请先在联网机器执行：python3 scripts/download_models.py
     或使用 --models-separate / --skip-models 跳过内嵌模型"
  fi
  COUNT="$(find "$MODEL_HOME/official_models" -mindepth 1 -maxdepth 1 -type d | wc -l | tr -d ' ')"
  ok "模型产线 $COUNT 条（$MODEL_HOME）"
fi
ok "构建环境就绪"

if [ "$DRY_RUN" -eq 1 ]; then
  step "dry-run：以下命令将被执行"
  for t in "${TARGET_LIST[@]}"; do
    os="$(target_os "$t")"; arch="$(target_arch_id "$t")"; dev="$(target_device "$t")"
    echo
    info "[$t]"
    [ "$SKIP_DOWNLOAD" -eq 0 ] && echo "  ./scripts/download_python_runtime.sh --os $os --arch $arch"
    if [ "$SKIP_DOWNLOAD" -eq 0 ]; then
      if [ "$dev" = "gpu" ]; then
        echo "  ./scripts/download_wheels.sh --os $os --arch $arch --device gpu --cuda $CUDA"
      else
        echo "  ./scripts/download_wheels.sh --os $os --arch $arch --python 3.12"
      fi
    fi
    if [ "$os" = "linux" ]; then
      echo "  ./scripts/make_offline_bundle.sh --arch $arch --device $dev$( [ "$SKIP_RUNTIME" -eq 1 ] && echo ' --skip-runtime')$( [ "$SKIP_MODELS" -eq 1 ] && echo ' --skip-models')"
    else
      echo "  ./scripts/make_platform_bundle.sh --os $os --arch $arch$( [ "$SKIP_RUNTIME" -eq 1 ] && echo ' --skip-runtime')$( [ "$SKIP_MODELS" -eq 1 ] && echo ' --skip-models')"
    fi
  done
  echo
  info "产物目录：$RELEASE_DIR"
  exit 0
fi

mkdir -p "$RELEASE_DIR"
BUILT_ARTIFACTS=()

artifact_for() {  # $1=target
  local t="$1"
  case "$t" in
    linux-amd64-gpu) echo "$PROJECT_ROOT/dist/paddlex-ocr-web-${VERSION}-amd64-gpu.tar.gz" ;;
    linux-*)         echo "$PROJECT_ROOT/dist/paddlex-ocr-web-${VERSION}-$(target_arch_id "$t").tar.gz" ;;
    windows-*)       echo "$PROJECT_ROOT/dist/paddlex-ocr-web-${VERSION}-${t}.zip" ;;
    *)               echo "$PROJECT_ROOT/dist/paddlex-ocr-web-${VERSION}-${t}.tar.gz" ;;
  esac
}

# ---------------------------------------------------------------- 逐平台构建
for t in "${TARGET_LIST[@]}"; do
  os="$(target_os "$t")"; arch="$(target_arch_id "$t")"; dev="$(target_device "$t")"
  step "构建 $t"

  if [ "$SKIP_DOWNLOAD" -eq 0 ]; then
    ./scripts/download_python_runtime.sh --os "$os" --arch "$arch"
    if [ "$dev" = "gpu" ]; then
      ./scripts/download_wheels.sh --os "$os" --arch "$arch" --device gpu --cuda "$CUDA"
    else
      ./scripts/download_wheels.sh --os "$os" --arch "$arch" --python 3.12
    fi
  else
    [ -d "$(target_wheels_dir "$t")" ] || die "--skip-download 但缺少 wheel 目录：$(target_wheels_dir "$t")"
  fi

  EXTRA=()
  [ "$SKIP_RUNTIME" -eq 1 ] && EXTRA+=(--skip-runtime)
  [ "$SKIP_MODELS" -eq 1 ] && EXTRA+=(--skip-models)

  if [ "$os" = "linux" ]; then
    ./scripts/make_offline_bundle.sh --arch "$arch" --device "$dev" \
      --wheels-dir "$(target_wheels_dir "$t")" "${EXTRA[@]+"${EXTRA[@]}"}"
  else
    ./scripts/make_platform_bundle.sh --os "$os" --arch "$arch" "${EXTRA[@]+"${EXTRA[@]}"}"
  fi

  ART="$(artifact_for "$t")"
  [ -f "$ART" ] || die "构建结束但找不到产物：$ART"
  mv -f "$ART" "$RELEASE_DIR/"
  [ -f "$ART.sha256" ] && mv -f "$ART.sha256" "$RELEASE_DIR/" || true
  BUILT_ARTIFACTS+=("$(basename "$ART")")
  ok "已产出 $(basename "$ART")"
done

# ---------------------------------------------------------------- 共享模型包
if [ "$MODELS_SEPARATE" -eq 1 ]; then
  step "生成共享模型包"
  MODELS_ARCHIVE="$RELEASE_DIR/models-paddlex_home-${VERSION}.tar.gz"
  MODEL_MEMBERS=(models/paddlex_home)
  [ -d "$PROJECT_ROOT/models/indra" ] && MODEL_MEMBERS+=(models/indra)
  [ -f "$PROJECT_ROOT/models/manifest.json" ] && MODEL_MEMBERS+=(models/manifest.json)
  [ -f "$PROJECT_ROOT/models/README.md" ] && MODEL_MEMBERS+=(models/README.md)
  COPYFILE_DISABLE=1 tar czf "$MODELS_ARCHIVE" -C "$PROJECT_ROOT" \
    --exclude='.DS_Store' --exclude='._*' \
    "${MODEL_MEMBERS[@]}" \
    || die "模型打包失败"
  BUILT_ARTIFACTS+=("$(basename "$MODELS_ARCHIVE")")
  info "模型包：$(du -sh "$MODELS_ARCHIVE" | awk '{print $1}')；解压到任意部署包根目录即可：tar xzf $(basename "$MODELS_ARCHIVE")"
fi

# ---------------------------------------------------------------- 清单与校验
step "生成 SHA256SUMS 与 release-manifest.json"
TARGETS_CSV="$(IFS=','; echo "${TARGET_LIST[*]}")"
if [ "$MODELS_SEPARATE" -eq 1 ]; then
  MODELS_MODE="separate"
elif [ "$SKIP_MODELS" -eq 1 ]; then
  MODELS_MODE="none"
else
  MODELS_MODE="embedded"
fi
export TARGETS_CSV RELEASE_DIR VERSION MODELS_MODE
"$PYTHON3" - "$TARGETS_CSV" <<'PY'
import datetime, hashlib, json, os, platform, sys
release_dir = os.environ["RELEASE_DIR"]
version = os.environ["VERSION"]
mode = os.environ["MODELS_MODE"]
targets = [t for t in os.environ["TARGETS_CSV"].split(",") if t]

def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

entries = []
for name in sorted(os.listdir(release_dir)):
    path = os.path.join(release_dir, name)
    if not os.path.isfile(path) or name in ("SHA256SUMS", "release-manifest.json"):
        continue
    entries.append({
        "file": name,
        "size_bytes": os.path.getsize(path),
        "sha256": sha256(path),
    })

target_map = {}
for t in targets:
    os_name, arch = t.split("-")[0], t.split("-")[1]
    device = "gpu" if t.endswith("-gpu") else "cpu"
    paddle = "3.4.0" if os_name == "linux" else "3.3.1"
    ext = ".zip" if os_name == "windows" else ".tar.gz"
    name = f"paddlex-ocr-web-{version}-{t}{ext}"
    if t == "linux-amd64-gpu":
        name = f"paddlex-ocr-web-{version}-amd64-gpu.tar.gz"
    elif t.startswith("linux-"):
        name = f"paddlex-ocr-web-{version}-{arch}{ext}"
    target_map[t] = {"os": os_name, "arch": arch, "device": device,
                     "paddle": paddle, "package": name, "models": mode}

manifest = {
    "name": "paddlex-ocr-web",
    "version": version,
    "generated_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
    "build_host": {"system": platform.system(), "machine": platform.machine(),
                   "python": platform.python_version()},
    "models_mode": mode,
    "targets": target_map,
    "artifacts": entries,
    "notes": [
        "Linux: Paddle 3.4.0, CPU/GPU; GPU only linux-amd64",
        "macOS: Paddle 3.3.1, Apple Silicon CPU only",
        "Windows: Paddle 3.3.1, x86_64 CPU only (needs VC++ 2015-2022 x64)",
        "Unsupported: macos-amd64, windows-arm64, non-x86_64 GPU",
    ],
}
with open(os.path.join(release_dir, "release-manifest.json"), "w", encoding="utf-8") as f:
    json.dump(manifest, f, ensure_ascii=False, indent=2)

sums = []
for e in entries:
    sums.append(f"{e['sha256']}  {e['file']}")
with open(os.path.join(release_dir, "SHA256SUMS"), "w", encoding="utf-8") as f:
    f.write("\n".join(sums) + "\n")
print(f"  产物 {len(entries)} 个，清单已写入 {release_dir}")
PY

cat > "$RELEASE_DIR/DEPLOY_ALL.txt" <<EOF
PaddleX OCR Web · 全平台离线部署包（版本 $VERSION）

校验（Linux/macOS）：
  cd $(basename "$RELEASE_DIR") && sha256sum -c SHA256SUMS
  macOS 可用：shasum -a 256 -c SHA256SUMS

平台选择：
  Linux x86_64 CPU : paddlex-ocr-web-${VERSION}-amd64.tar.gz
  Linux arm64 CPU  : paddlex-ocr-web-${VERSION}-arm64.tar.gz
  Linux x86_64 GPU : paddlex-ocr-web-${VERSION}-amd64-gpu.tar.gz（需 NVIDIA 驱动 >= 575.57.08）
  macOS arm64 CPU  : paddlex-ocr-web-${VERSION}-macos-arm64.tar.gz（macOS 11+，Paddle 3.3.1）
  Windows x64 CPU  : paddlex-ocr-web-${VERSION}-windows-amd64.zip（Win10 1803+，需 VC++ x64）

安装：
  Linux/macOS : tar xzf <包>.tar.gz && cd <目录> && ./scripts/install_offline.sh && ./scripts/start.sh --daemon
  Windows     : 解压 zip，然后
                powershell -ExecutionPolicy Bypass -File scripts\\install_windows.ps1
                powershell -ExecutionPolicy Bypass -File scripts\\start_windows.ps1

模型说明（models_mode=$MODELS_MODE）：
  内嵌：安装即用。
  单独：先 tar xzf models-paddlex_home-${VERSION}.tar.gz -C <部署包根目录>，再执行安装。
  不含：安装后把 official_models 放到 models/paddlex_home/。

不支持的组合（上游无轮子）：
  macOS Intel、Windows ARM64、Linux ARM64 GPU。
  上述场景请改用 Docker（docker/）或对应平台的 NGC/Paddle 官方镜像。
EOF

step "完成"
ok "发布目录：$RELEASE_DIR"
ls -lh "$RELEASE_DIR"
info "分发前建议把整个目录打成单一 tar：tar czf $(basename "$RELEASE_DIR").tar.gz -C $(dirname "$RELEASE_DIR") $(basename "$RELEASE_DIR")"
