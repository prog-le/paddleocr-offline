"""补全 paddlepaddle-gpu 的 Linux 专属依赖（无需显卡）。

问题背景
--------
`paddlepaddle-gpu` 的 `Requires-Dist` 里有一批这样的条目：

    nvidia-cudnn-cu12==9.9.0.52; platform_system == "Linux" and platform_machine == "x86_64"

在 macOS（或 arm64 Linux）上执行 `pip download --platform linux_x86_64` 时，
pip **按当前解释器**判断环境标记，于是把这些 Linux 专属依赖全部跳过，
打出来的离线目录看似完整，实际到 x86_64 容器里安装就会报：

    Could not find a version that satisfies the requirement nvidia-cuda-nvrtc-cu12==12.9.41

本脚本的作用：扫描目录里所有 wheel 的 METADATA，收集带 Linux 标记的依赖，
按 wheel 文件名判断哪些还没下载，把缺失的 requirement 字符串逐行打印给 shell，
由 `download_wheels.sh` 用 `--no-deps` 精确补下（并循环几轮处理传递依赖）。

用法：
    python3 scripts/gpu_deps.py --dir offline/wheels-gpu/amd64
    # 没有缺失时输出为空，退出码 0
"""

from __future__ import annotations

import argparse
import re
import sys
import zipfile
from pathlib import Path
from typing import Iterable, List, Set

# 与 PEP 503 一致的名称归一化
_NORM_RE = re.compile(r"[-_.]+")


def norm(name: str) -> str:
    return _NORM_RE.sub("_", name).lower()


def dist_name_of(wheel: Path) -> str:
    """从 wheel 文件名取分发名（第一个 '-' 之前的部分）。"""
    return norm(wheel.name.split("-")[0])


_REQ_RE = re.compile(r"^\s*Requires-Dist:\s*(.+?)\s*$", re.I)
_SYS_PLATFORM_IS = re.compile(r"sys_platform\s*(?:==|===)\s*['\"]([a-z0-9_]+)['\"]")


def _metadata_text(wheel: Path) -> str:
    try:
        with zipfile.ZipFile(wheel) as zf:
            for name in zf.namelist():
                if name.endswith(".dist-info/METADATA"):
                    return zf.read(name).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - 坏文件交给上层校验脚本处理
        return ""
    return ""


_PY_CMP_RE = re.compile(
    r"(python_full_version|python_version)\s*(===|==|!=|>=|<=|~=|>|<)\s*['\"]?([0-9][0-9.*]*)['\"]?"
)


def _vtuple(text: str) -> tuple:
    parts = []
    for piece in text.split("."):
        if piece.isdigit():
            parts.append(int(piece))
        else:
            break
    return tuple(parts) if parts else (0,)


def _cmp_ok(op: str, have: tuple, want_text: str) -> bool:
    """比较目标解释器版本与标记里的版本（够用即可，不做完整 PEP 440 实现）。"""
    want = _vtuple(want_text)
    width = max(len(have), len(want))
    a = have + (0,) * (width - len(have))
    b = want + (0,) * (width - len(want))
    if op in ("==", "==="):
        return a == b
    if op == "!=":
        return a != b
    if op == ">=":
        return a >= b
    if op == "<=":
        return a <= b
    if op == ">":
        return a > b
    if op == "<":
        return a < b
    if op == "~=":                    # ~=3.12 == >=3.12, ==3.*
        return a >= b and a[: len(want) - 1] == b[: len(want) - 1]
    return True


def python_marker_ok(marker: str, target_python: str) -> bool:
    """按**目标解释器**（而非运行本脚本的解释器）判断 python_version 条件。

    为什么需要：`pip download` 的环境标记是按**当前解释器**求值的。在 python3.10 的
    下载机上，`setuptools; python_version >= "3.12"`（paddlepaddle-gpu 的真实依赖）
    会被判为 false 而漏下，到 cp312 的目标机安装时才暴露。

    无 python_version 条件 -> True；含 `or` 等复杂组合 -> True（宁可多下一个包，
    也不要漏），保证不会误删依赖。
    """
    if not marker:
        return True
    low = marker.lower()
    if "python_version" not in low and "python_full_version" not in low:
        return True
    if " or " in low:
        return True
    have = _vtuple(target_python)
    for m in _PY_CMP_RE.finditer(low):
        op, want = m.group(2), m.group(3)
        if not _cmp_ok(op, have, want):
            return False
    return True


def needed_requirements(wheel: Path, target_python: str = "3.12") -> List[str]:
    """返回目标平台（Linux x86_64 / 指定 Python 版本）真正需要的依赖（去掉 marker）。

    三类都要考虑：
      * 带 `platform_system == "Linux"` 标记的（nvidia-* / cuda-python）——
        在 macOS 上 `pip download --platform` 会被跳过，是本项目踩过的坑；
      * 完全没有标记的传递依赖（如 cuda-python -> cuda-bindings）——
        它们不会被 Linux 标记过滤，但如果用 `--no-deps` 精确补包就会漏；
      * 带 `python_version` 条件的（如 `setuptools; python_version >= "3.12"`）——
        按目标解释器判断，避免下载机 Python 版本更老时漏包。
    跳过：extras、Windows/macOS/aarch64 专属。
    """
    out: List[str] = []
    for line in _metadata_text(wheel).splitlines():
        m = _REQ_RE.match(line)
        if not m:
            continue
        spec = m.group(1).strip()
        requirement, marker = (spec.split(";", 1) + [""])[:2]
        requirement = requirement.strip()
        low = marker.lower()
        if not requirement:
            continue
        if "extra" in low:
            continue
        # 目标平台固定为 linux/x86_64：其它系统/架构的标记依赖在本机根本用不到，
        # 若算作"缺失"会让补包循环反复尝试一个装不上的包。
        if "win32" in low or "windows" in low or "darwin" in low or "macos" in low:
            continue
        if "aarch64" in low or "arm64" in low or "armv7l" in low:
            continue
        # sys_platform 只接受 linux（emscripten / wasi / cygwin 等一律跳过）
        if any(value != "linux" for value in _SYS_PLATFORM_IS.findall(low)):
            continue
        if not python_marker_ok(marker, target_python):
            continue
        out.append(requirement)
    return out


def requirement_name(req: str) -> str:
    """从 `nvidia-cudnn-cu12==9.9.0.52` 里取出分发名。"""
    name = re.split(r"[<>=!~\[ ]", req, maxsplit=1)[0].strip()
    return norm(name)


def _version_of(wheel: Path) -> str:
    parts = wheel.name.split("-")
    return parts[1] if len(parts) > 1 else ""


def _version_matches(requirement: str, versions: Set[str]) -> bool:
    """只精确处理 `==` 与 `~=`（覆盖 nvidia-* 的固定版本场景）。

    其它比较符（>=、<、!=）无法在不依赖 packaging 的情况下可靠比较，
    按「名字存在即认为满足」处理——宁可漏报，也不误报导致反复下载。
    """
    m = re.search(r"==\s*([0-9][^*]*)", requirement)
    if m and "*" not in requirement:
        want = m.group(1).strip()
        return want in versions
    m = re.search(r"~=\s*([0-9][^,;\s]*)", requirement)
    if m:
        base = m.group(1).strip()
        # PEP 440：~=X.Y.Z 等价于 >=X.Y.Z, ==X.Y.*；~=X.Y 等价于 >=X.Y, ==X.*
        parts = base.split(".")
        prefix = ".".join(parts[:-1]) if len(parts) > 1 else parts[0]
        return any(v == base or v.startswith(prefix + ".") or v == prefix for v in versions)
    return True


def collect(directory: Path, target_python: str = "3.12") -> Set[str]:
    present: dict = {}
    for wheel in directory.glob("*.whl"):
        present.setdefault(dist_name_of(wheel), set()).add(_version_of(wheel))

    missing: Set[str] = set()
    for wheel in directory.glob("*.whl"):
        for req in needed_requirements(wheel, target_python):
            name = requirement_name(req)
            versions = present.get(name)
            if not versions or not _version_matches(req, versions):
                missing.add(req)
    return missing


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="列出 GPU 版 Paddle 在离线目录中缺失的依赖")
    parser.add_argument("--dir", required=True, help="wheel 目录")
    parser.add_argument(
        "--target-python",
        default="3.12",
        help="目标解释器版本（用于判断 python_version 环境标记，默认 3.12）",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    directory = Path(args.dir)
    if not directory.is_dir():
        print(f"[失败] 目录不存在：{directory}", file=sys.stderr)
        return 2

    missing = sorted(collect(directory, args.target_python))
    for req in missing:
        print(req)
    if missing:
        print(f"[信息] 缺失 {len(missing)} 个依赖", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
