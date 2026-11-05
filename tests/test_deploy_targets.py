"""跨平台部署目标矩阵单测（不需要网络/模型/GPU）。

覆盖：
  * 支持/不支持的目标组合（Linux amd64/arm64、Linux GPU、macOS arm64、Windows x64）
  * 每个目标的 OS / 架构 / 设备解析
  * wheel 与随包运行时目录约定（Linux 沿用 <arch>，其他平台用 <target>）
  * 随包 CPython 运行时的 triple 映射
  * requirements 文件选择
  * 一键打包入口的 --list-targets / --dry-run / 拒绝不支持目标
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_SH = ROOT / "scripts" / "env.sh"
BUILD = ROOT / "scripts" / "build_deploy_package.sh"

FAILED = 0

SUPPORTED = ["linux-amd64", "linux-arm64", "linux-amd64-gpu", "macos-arm64", "windows-amd64"]
UNSUPPORTED = ["macos-amd64", "windows-arm64", "linux-arm64-gpu"]


def check(name: str, ok: bool, detail: str = "") -> None:
    global FAILED
    if ok:
        print(f"  [通过] {name}")
    else:
        FAILED += 1
        print(f"  [失败] {name}  {detail}")


def bash(code: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", code], cwd=ROOT, capture_output=True, text=True)


def call_env(fn: str, *args: str) -> str:
    quoted = " ".join(f"'{a}'" for a in args)
    proc = bash(f'source "{ENV_SH}"; {fn} {quoted}')
    if proc.returncode != 0:
        return f"__ERROR__:{proc.stderr.strip()}"
    return proc.stdout.strip()


def relative(path: str) -> str:
    return path.replace(str(ROOT) + "/", "")


def main() -> int:
    print("目标支持矩阵")
    for target in SUPPORTED:
        proc = bash(f'source "{ENV_SH}"; target_supported "{target}" && echo yes || echo no')
        check(f"{target} 受支持", proc.stdout.strip() == "yes")
    for target in UNSUPPORTED:
        proc = bash(f'source "{ENV_SH}"; target_supported "{target}" && echo yes || echo no')
        check(f"{target} 不受支持", proc.stdout.strip() == "no")

    print("\n目标解析")
    expect = {
        "linux-amd64": ("linux", "amd64", "cpu"),
        "linux-arm64": ("linux", "arm64", "cpu"),
        "linux-amd64-gpu": ("linux", "amd64", "gpu"),
        "macos-arm64": ("macos", "arm64", "cpu"),
        "windows-amd64": ("windows", "amd64", "cpu"),
    }
    for target, (os_name, arch, device) in expect.items():
        got = (
            call_env("target_os", target),
            call_env("target_arch_id", target),
            call_env("target_device", target),
        )
        check(f"{target} 解析为 {os_name}/{arch}/{device}", got == (os_name, arch, device), str(got))

    print("\n目录与运行时")
    dir_expect = {
        "linux-amd64": ("offline/wheels/amd64", "offline/python/amd64", "x86_64-unknown-linux-gnu"),
        "linux-arm64": ("offline/wheels/arm64", "offline/python/arm64", "aarch64-unknown-linux-gnu"),
        "linux-amd64-gpu": ("offline/wheels-gpu/amd64", "offline/python/amd64", "x86_64-unknown-linux-gnu"),
        "macos-arm64": ("offline/wheels/macos-arm64", "offline/python/macos-arm64", "aarch64-apple-darwin"),
        "windows-amd64": ("offline/wheels/windows-amd64", "offline/python/windows-amd64", "x86_64-pc-windows-msvc"),
    }
    for target, (wheels, runtime, triple) in dir_expect.items():
        got = (
            relative(call_env("target_wheels_dir", target)),
            relative(call_env("target_runtime_dir", target)),
            call_env("target_runtime_triple", target),
        )
        check(f"{target} 目录/triple 正确", got == (wheels, runtime, triple), str(got))

    print("\n依赖文件选择")
    req_expect = {
        "linux-amd64": "requirements.txt",
        "linux-arm64": "requirements.txt",
        "linux-amd64-gpu": "requirements.txt",
        "macos-arm64": "requirements-macos.txt",
        "windows-amd64": "requirements-windows.txt",
    }
    for target, req in req_expect.items():
        check(f"{target} -> {req}", call_env("target_requirements", target) == req)

    print("\n不支持目标的提示")
    notes = {
        "macos-amd64": "macOS Intel",
        "windows-arm64": "Windows ARM64",
        "linux-arm64-gpu": "linux x86_64",
    }
    for target, keyword in notes.items():
        note = call_env("target_support_note", target)
        check(f"{target} 提示含 {keyword}", keyword in note, note)

    print("\n一键打包入口")
    proc = bash(f'"{BUILD}" --list-targets')
    check("--list-targets 退出码 0", proc.returncode == 0, proc.stderr[-300:])
    for target in SUPPORTED:
        check(f"--list-targets 含 {target}", target in proc.stdout)
    check("--list-targets 标注不支持组合", "macos-amd64" in proc.stdout and "windows-arm64" in proc.stdout)

    proc = bash(
        f'"{BUILD}" --all --with-gpu --dry-run --skip-download --skip-models'
    )
    check("全平台 dry-run 退出码 0", proc.returncode == 0, proc.stderr[-300:])
    for needle in (
        "make_offline_bundle.sh --arch amd64 --device cpu",
        "make_offline_bundle.sh --arch arm64 --device cpu",
        "make_offline_bundle.sh --arch amd64 --device gpu",
        "make_platform_bundle.sh --os macos --arch arm64",
        "make_platform_bundle.sh --os windows --arch amd64",
    ):
        check(f"dry-run 含命令：{needle}", needle in proc.stdout)
    check("dry-run 不含 unknown 架构", "arch=unknown" not in proc.stdout and "--arch unknown" not in proc.stdout)

    proc = bash(f'"{BUILD}" --targets macos-amd64 --dry-run --skip-download --skip-models')
    check("拒绝 macOS Intel", proc.returncode != 0 and "macOS Intel" in (proc.stdout + proc.stderr))

    proc = bash(f'"{BUILD}" --targets windows-arm64 --dry-run --skip-download --skip-models')
    check("拒绝 Windows ARM64", proc.returncode != 0 and "Windows ARM64" in (proc.stdout + proc.stderr))

    print()
    if FAILED:
        print(f"失败 {FAILED} 项")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
