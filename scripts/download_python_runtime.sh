#!/usr/bin/env bash
# =============================================================================
# 随包 Python 运行时下载（在【有外网】的机器上执行）
#
# 目标机没有 python3（或版本过旧）时，把一份**可重定位的 CPython 3.12** 打进交付包：
#   * 来源：astral-sh/python-build-standalone 的 install_only 构建
#     （Linux glibc ≥ 2.17；macOS / Windows 同样提供 install_only 资产）
#   * 落盘：offline/python/<target>/cpython-3.12.x+<release>-<triple>-install_only.tar.gz
#     Linux 沿用 offline/python/<arch>；macOS/Windows 用 offline/python/<os>-<arch>
#   * 安装时由 install_offline.sh（Windows 用 install_windows.ps1）解包到
#     <项目>/.runtime/python/ 并用它建 venv，不依赖目标机的 python3。
#
# 用法：
#   ./scripts/download_python_runtime.sh                    # 当前平台（Linux 默认双架构）
#   ./scripts/download_python_runtime.sh --os linux         # Linux amd64+arm64
#   ./scripts/download_python_runtime.sh --os macos --arch arm64
#   ./scripts/download_python_runtime.sh --os windows --arch amd64
#   ./scripts/download_python_runtime.sh --python 3.12.15   # 指定补丁版本
#   ./scripts/download_python_runtime.sh --release 20261021 # 固定 release（可复现）
#   ./scripts/download_python_runtime.sh --list             # 只列出匹配的资产
#
# 网络说明：部分内网/代理会拦截 github.com 的 release 资源（返回 502）。
# 脚本会按下面的顺序自动重试（可用环境变量 PY_RUNTIME_MIRRORS 覆盖，空格分隔）：
#   1) https://github.com                                              （直连）
#   2) https://gh-proxy.com/https://github.com                         （加速镜像）
#   3) https://ghproxy.net/https://github.com                          （加速镜像）
#   export PY_RUNTIME_MIRRORS="https://my-mirror.example.com/https://github.com"
# =============================================================================
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

PY_SERIES="3.12"
PY_PATCH=""
RELEASE="latest"
ARCHES=()
TARGET_OS="linux"
OUT_ROOT="$PROJECT_ROOT/offline/python"
LIST_ONLY=0

while [ $# -gt 0 ]; do
  case "$1" in
    --os) TARGET_OS="$2"; shift 2 ;;
    --os=*) TARGET_OS="${1#*=}"; shift ;;
    --arch) ARCHES+=("$2"); shift 2 ;;
    --arch=*) ARCHES+=("${1#*=}"); shift ;;
    --python) PY_PATCH="$2"; shift 2 ;;
    --python=*) PY_PATCH="${1#*=}"; shift ;;
    --release) RELEASE="$2"; shift 2 ;;
    --release=*) RELEASE="${1#*=}"; shift ;;
    --out) OUT_ROOT="$2"; shift 2 ;;
    --list) LIST_ONLY=1; shift ;;
    -h|--help) sed -n '2,31p' "$0"; exit 0 ;;
    *) die "未知参数：$1" ;;
  esac
done

if [ -n "$PY_PATCH" ]; then
  PY_SERIES="$(echo "$PY_PATCH" | cut -d. -f1,2)"
fi
case "$TARGET_OS" in
  linux)   [ ${#ARCHES[@]} -gt 0 ] || ARCHES=(amd64 arm64) ;;
  macos)   [ ${#ARCHES[@]} -gt 0 ] || ARCHES=(arm64) ;;
  windows) [ ${#ARCHES[@]} -gt 0 ] || ARCHES=(amd64) ;;
  *) die "不支持的 --os：$TARGET_OS（可选 linux / macos / windows）" ;;
esac
# 先校验目标矩阵，避免在不支持的组合上浪费网络请求
for _arch in "${ARCHES[@]}"; do
  _target="${TARGET_OS}-${_arch}"
  target_supported "$_target" || die "$(target_support_note "$_target")"
done
require_cmd curl "下载运行时需要 curl（或改用 wget）"
PY="$(command -v python3 || true)"
[ -n "$PY" ] || die "需要 python3 解析 GitHub release 元数据（下载机应有 python3）"

case "$RELEASE" in
  latest) API="https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest" ;;
  *)      API="https://api.github.com/repos/astral-sh/python-build-standalone/releases/tags/$RELEASE" ;;
esac

# GitHub 资源前缀（直连优先，失败后逐个镜像重试）
if [ -n "${PY_RUNTIME_MIRRORS:-}" ]; then
  read -r -a MIRRORS <<< "$PY_RUNTIME_MIRRORS"
else
  MIRRORS=(https://github.com https://gh-proxy.com/https://github.com https://ghproxy.net/https://github.com)
fi

# 把 github.com 开头的 URL 换成镜像前缀；$1=某个前缀 $2=原始 URL
mirror_url() {
  local prefix="$1" url="$2"
  if [ "$prefix" = "https://github.com" ]; then
    echo "$url"
  else
    echo "${prefix%/}/${url#https://github.com/}"
  fi
}

# 依次尝试各前缀下载；成功返回 0
fetch_with_fallback() {  # $1=原 URL  $2=输出文件
  local url="$1" out="$2" prefix real code
  for prefix in "${MIRRORS[@]}"; do
    real="$(mirror_url "$prefix" "$url")"
    info "尝试：$real"
    code="$(curl -fL --retry 2 --retry-delay 2 --connect-timeout 20 --max-time 1800 \
                 --progress-bar -o "$out.part" -w '%{http_code}' "$real" 2>/dev/null || true)"
    if [ -s "$out.part" ]; then
      mv "$out.part" "$out"
      return 0
    fi
    rm -f "$out.part"
    warn "失败（HTTP ${code:-?}），换下一个源"
  done
  return 1
}

print_banner "随包 Python 运行时下载"

step "1/3 查询 release 元数据"
META="$(mktemp)"
trap 'rm -f "$META"' EXIT
curl -fsSL --max-time 60 -H 'Accept: application/vnd.github+json' "$API" -o "$META" \
  || die "无法访问 GitHub API（${API}）。可手动下载后放到 $OUT_ROOT/<arch>/ 下，文件见脚本头部说明。"
TAG="$("$PY" - "$META" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
print(d.get("tag_name") or "")
PY
)"
[ -n "$TAG" ] || die "release 元数据异常（没有 tag_name）"
ok "release: $TAG"

# 解析目标 triple 对应的资产名与下载地址
resolve_asset() {  # $1=triple  ->  name<TAB>url<TAB>sha_url
  "$PY" - "$META" "$1" "$PY_SERIES" "$PY_PATCH" <<'PY'
import json, sys
meta, triple, series, patch = sys.argv[1:5]
d = json.load(open(meta, encoding="utf-8"))
want_prefix = f"cpython-{patch}+" if patch else f"cpython-{series}."
best = None
for a in d.get("assets", []):
    n = a["name"]
    if not n.startswith(want_prefix):
        continue
    if not n.endswith(f"-{triple}-install_only.tar.gz"):
        continue
    best = a
if not best:
    sys.exit(3)
print(best["name"] + "\t" + best["browser_download_url"])
PY
}

# release 只发布一个 SHA256SUMS 汇总文件（单个资产没有 .sha256）
SUMS_FILE="$OUT_ROOT/SHA256SUMS"
SUMS_URL="https://github.com/astral-sh/python-build-standalone/releases/download/$TAG/SHA256SUMS"
if [ "$LIST_ONLY" -eq 0 ] && [ -n "${TAG}" ]; then
  mkdir -p "$OUT_ROOT"
  if [ -s "$SUMS_FILE" ] && grep -q . "$SUMS_FILE"; then
    info "复用已有 SHA256SUMS"
  else
    fetch_with_fallback "$SUMS_URL" "$SUMS_FILE" >/dev/null 2>&1 \
      || warn "未能获取 SHA256SUMS，将只记录本地计算的 sha256"
  fi
fi

step "2/3 下载"
for ARCH in "${ARCHES[@]}"; do
  TARGET="${TARGET_OS}-${ARCH}"
  target_supported "$TARGET" || die "$(target_support_note "$TARGET")"
  TRIPLE="$(target_runtime_triple "$TARGET")" || die "没有 ${TARGET} 对应的运行时 triple"
  if ! ROW="$(resolve_asset "$TRIPLE")"; then
    die "release $TAG 中找不到 ${TARGET}（${TRIPLE}）的 CPython $([ -n "$PY_PATCH" ] && echo "$PY_PATCH" || echo "$PY_SERIES.x") install_only 资产"
  fi
  NAME="$(echo "$ROW" | cut -f1)"
  URL="$(echo "$ROW" | cut -f2)"

  if [ "$LIST_ONLY" -eq 1 ]; then
    printf '  %-14s %s\n               %s\n' "$TARGET" "$NAME" "$URL"
    continue
  fi

  # Linux 沿用 offline/python/<arch>；macOS/Windows 用 offline/python/<target>
  if [ "$TARGET_OS" = "linux" ]; then
    DEST="$OUT_ROOT/$ARCH"
  else
    DEST="$OUT_ROOT/$TARGET"
  fi
  mkdir -p "$DEST"
  if [ -f "$DEST/$NAME" ]; then
    info "$TARGET 已存在：${NAME}（跳过下载）"
  else
    info "$TARGET 下载 ${NAME}"
    fetch_with_fallback "$URL" "$DEST/$NAME" \
      || die "多个源均下载失败。可手动下载后放到 $DEST/：
       $URL
     或设置镜像：export PY_RUNTIME_MIRRORS=\"https://your-mirror/https://github.com\""
  fi

  # 结构校验：防止因资产名匹配到错误平台
  # 先落清单再 grep（pipefail 下 `tar tzf | grep -q` 会因 SIGPIPE 误报）
  RT_LISTING="$(mktemp "${TMPDIR:-/tmp}/pdx-rt-list.XXXXXX")"
  if ! tar tzf "$DEST/$NAME" > "$RT_LISTING" 2>/dev/null; then
    rm -f "$RT_LISTING"
    die "$NAME 无法读取（下载可能不完整）"
  fi
  if ! grep -E 'python/(bin/python3|python\.exe)' "$RT_LISTING" >/dev/null; then
    rm -f "$RT_LISTING"
    die "$NAME 结构异常：解包后找不到 python/bin/python3 或 python/python.exe"
  fi
  rm -f "$RT_LISTING"

  # 校验：官方 SHA256SUMS（每行 "<hash>  <filename>"）
  if command -v shasum >/dev/null 2>&1; then
    ACTUAL="$(shasum -a 256 "$DEST/$NAME" | awk '{print $1}')"
  else
    ACTUAL="$(sha256sum "$DEST/$NAME" | awk '{print $1}')"
  fi
  EXPECT=""
  if [ -s "$SUMS_FILE" ]; then
    EXPECT="$(awk -v n="$NAME" '$2==n {print $1; exit}' "$SUMS_FILE")"
  fi
  if [ -n "$EXPECT" ]; then
    [ "$EXPECT" = "$ACTUAL" ] || die "$TARGET sha256 不匹配（期望 ${EXPECT}，实际 ${ACTUAL}）"
    ok "$TARGET sha256 校验通过"
  else
    warn "$TARGET 无官方校验值，已记录本地 sha256：$ACTUAL"
  fi
  echo "$ACTUAL  $NAME" > "$DEST/$NAME.sha256"

  SIZE="$(du -sh "$DEST/$NAME" | awk '{print $1}')"
  # 运行时与 CPU 架构强相关，但与本机架构无关，可在任意机器下载
  cat > "$DEST/runtime-info.txt" <<EOF
file=$NAME
os=$TARGET_OS
target=$TARGET
release=$TAG
python=$(echo "$NAME" | sed -E 's/^cpython-([0-9.]+)\+.*/\1/')
triple=$TRIPLE
size=$SIZE
sha256=$ACTUAL
$( [ "$TARGET_OS" = "linux" ] && echo "glibc_min=2.17" )
source=$URL
EOF
  ok "$TARGET 完成：${NAME}（${SIZE}）"
done

[ "$LIST_ONLY" -eq 1 ] && exit 0

step "3/3 汇总"
for ARCH in "${ARCHES[@]}"; do
  if [ "$TARGET_OS" = "linux" ]; then DEST="$OUT_ROOT/$ARCH"; else DEST="$OUT_ROOT/${TARGET_OS}-${ARCH}"; fi
  [ -d "$DEST" ] || continue
  printf '  %-14s %s\n' "${TARGET_OS}-${ARCH}" "$(ls -1 "$DEST"/*.tar.gz 2>/dev/null | xargs -n1 basename 2>/dev/null | tr '\n' ' ')"
done

cat <<EOF

下一步：
  1) 下载离线 wheel（必须与运行时同 Python 版本）：
       ./scripts/download_wheels.sh --os ${TARGET_OS} --arch ${ARCHES[0]} --python ${PY_SERIES}
  2) 打包：./scripts/build_deploy_package.sh --targets ${TARGET_OS}-${ARCHES[0]}
  3) 目标机安装（Linux/macOS: ./scripts/install_offline.sh；Windows: scripts/install_windows.ps1）
EOF
