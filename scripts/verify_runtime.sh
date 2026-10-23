#!/usr/bin/env bash
# =============================================================================
# 校验随包 Python 运行时（在任意机器上都能跑，不需要 Linux）
#
# 检查项：
#   1) 压缩包存在、大小合理
#   2) sha256 与 download_python_runtime.sh 记录的（= 官方 SHA256SUMS）一致
#   3) 包内结构：python/bin/python3 / python3.12、stdlib、ensurepip 自带 pip
#   4) python 可执行文件的 ELF 架构与目标架构一致（aarch64 / x86_64）
#   5) 二进制里出现的最高 GLIBC 版本 ≤ 2.17（超出则老系统跑不起来）
#
# 用法：
#   ./scripts/verify_runtime.sh                 # 校验 offline/python 下的全部架构
#   ./scripts/verify_runtime.sh --arch arm64    # 只校验某一架构
# =============================================================================
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

ARCHES=()
while [ $# -gt 0 ]; do
  case "$1" in
    --arch) ARCHES+=("$2"); shift 2 ;;
    --arch=*) ARCHES+=("${1#*=}"); shift ;;
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
    *) die "未知参数：$1" ;;
  esac
done
[ ${#ARCHES[@]} -gt 0 ] || ARCHES=(amd64 arm64)

FAIL=0
note_fail() { warn "$*"; FAIL=$((FAIL+1)); }

print_banner "随包运行时校验"

for ARCH in "${ARCHES[@]}"; do
  DIR="$PROJECT_ROOT/offline/python/$ARCH"
  step "架构 $ARCH"
  if [ ! -d "$DIR" ]; then
    note_fail "缺少目录：${DIR}（执行 ./scripts/download_python_runtime.sh --arch ${ARCH}）"
    continue
  fi
  TARBALL="$(ls -1 "$DIR"/*.tar.gz 2>/dev/null | head -1 || true)"
  if [ -z "$TARBALL" ]; then
    note_fail "目录中没有 .tar.gz：$DIR"
    continue
  fi
  ok "压缩包：$(basename "$TARBALL")（$(du -sh "$TARBALL" | awk '{print $1}')）"

  # ---- 1) sha256 ----
  EXPECT_FILE="$TARBALL.sha256"
  if [ -f "$EXPECT_FILE" ]; then
    EXPECT="$(awk '{print $1}' "$EXPECT_FILE" | head -1)"
    if command -v shasum >/dev/null 2>&1; then
      ACTUAL="$(shasum -a 256 "$TARBALL" | awk '{print $1}')"
    else
      ACTUAL="$(sha256sum "$TARBALL" | awk '{print $1}')"
    fi
    if [ "$EXPECT" = "$ACTUAL" ]; then
      ok "sha256 一致：$ACTUAL"
    else
      note_fail "sha256 不匹配（记录 ${EXPECT}，实际 ${ACTUAL}）"
    fi
  else
    warn "没有 .sha256 记录文件，跳过一致性校验"
  fi

  # ---- 2) 包内结构 ----
  LIST="$(tar tzf "$TARBALL")"
  for need in "python/bin/python3" "python/bin/python3.12" "python/lib/python3.12/os.py"; do
    if grep -qx "$need" <<< "$LIST"; then
      ok "包含 $need"
    else
      note_fail "缺少 ${need}（运行时包结构异常）"
    fi
  done
  if grep -qE "^python/lib/python3\.12/ensurepip/_bundled/pip-.*\.whl$" <<< "$LIST"; then
    ok "自带 ensurepip 的 pip wheel（无需联网建 venv）"
  else
    note_fail "缺少 ensurepip 内置 pip wheel，venv 可能无法准备 pip"
  fi

  # ---- 3) ELF 架构 + 4) GLIBC 版本要求 ----
  if ! command -v python3 >/dev/null 2>&1; then
    warn "本机没有 python3，跳过 ELF/GLIBC 检查"
    continue
  fi
  tar -xzOf "$TARBALL" python/bin/python3.12 > /tmp/.rt_py_bin 2>/dev/null || true
  if [ ! -s /tmp/.rt_py_bin ]; then
    note_fail "无法从压缩包读取 python/bin/python3.12"
    continue
  fi
  python3 - "amd64|$ARCH" /tmp/.rt_py_bin <<'PY'
import re, sys

expected = {"amd64": 0x3E, "arm64": 0xB7}[sys.argv[1].split("|")[1]]
path = sys.argv[2]
data = open(path, "rb").read()

if data[:4] != b"\x7fELF":
    print("  [失败] python/bin/python3.12 不是 ELF 可执行文件")
    raise SystemExit(1)

e_machine = int.from_bytes(data[18:20], "little")
names = {0x3E: "x86_64", 0xB7: "aarch64"}
if e_machine != expected:
    print(f"  [失败] ELF 架构不符：期望 {names.get(expected)}，实际 {names.get(e_machine, hex(e_machine))}")
    raise SystemExit(1)
print(f"  [成功] ELF 架构正确：{names[e_machine]}")

# GLIBC 版本要求：扫描二进制中出现的 GLIBC_x.yy 字符串，取最大值
versions = sorted({(int(m.group(1)), int(m.group(2)))
                   for m in re.finditer(rb"GLIBC_(\d+)\.(\d+)", data)})
versions = [v for v in versions if v[0] == 2]
if not versions:
    print("  [信息] 未发现 GLIBC 版本标记（可能是静态链接）")
    raise SystemExit(0)
top = versions[-1]
print(f"  [信息] 最高 GLIBC 要求：{top[0]}.{top[1]}")
if (top[0], top[1]) > (2, 17):
    print(f"  [失败] 运行时要求 GLIBC_{top[0]}.{top[1]}，高于宣称的 2.17；老系统（如 CentOS 7 / buster）会加载失败")
    raise SystemExit(1)
print("  [成功] GLIBC 要求 ≤ 2.17，可在 CentOS 7 及以上 / Debian 10+ / Ubuntu 18.04+ 运行")
PY
  [ $? -eq 0 ] || note_fail "$ARCH 的二进制度检查未通过"
  rm -f /tmp/.rt_py_bin

  [ -f "$DIR/runtime-info.txt" ] && sed 's/^/  /' "$DIR/runtime-info.txt"
done

echo
if [ "$FAIL" -gt 0 ]; then
  warn "运行时校验未通过（$FAIL 项），请检查上面的告警"
  exit 1
fi
ok "运行时校验通过：${ARCHES[*]}"
