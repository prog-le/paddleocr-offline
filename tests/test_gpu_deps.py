"""GPU 依赖闭包检查单测（不需要显卡、不需要网络）。

覆盖线上踩过的真实问题：
  * macOS 上 `pip download --platform linux_x86_64` 会按当前解释器判断
    `platform_system == "Linux"` 标记，导致 nvidia-* / cuda-python 被跳过；
  * `cuda-python -> cuda-bindings` 这类**无标记**传递依赖，在 `--no-deps` 补包时会漏；
  * Windows / macOS 专属依赖和 extras 不应被误报。
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

spec = importlib.util.spec_from_file_location("gpu_deps", ROOT / "scripts" / "gpu_deps.py")
assert spec and spec.loader
gpu_deps = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gpu_deps)

FAILED = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global FAILED
    if ok:
        print(f"  [通过] {name}")
    else:
        FAILED += 1
        print(f"  [失败] {name}  {detail}")


def make_wheel(directory: Path, filename: str, requires: list[str]) -> Path:
    path = directory / filename
    meta = "Metadata-Version: 2.1\nName: x\nVersion: 1\n"
    meta += "".join(f"Requires-Dist: {r}\n" for r in requires)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(f"{filename.split('-')[0]}-1.dist-info/METADATA", meta)
    return path


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)

        make_wheel(
            d,
            "paddlepaddle_gpu-3.4.0-cp312-cp312-linux_x86_64.whl",
            [
                "numpy>=1.21",
                "httpx",
                # 真实轮子里的 Linux 标记依赖
                'nvidia-cudnn-cu12==9.9.0.52; platform_system == "Linux" and platform_machine == "x86_64"',
                'nvidia-cuda-nvrtc-cu12==12.9.41; platform_system == "Linux" and platform_machine == "x86_64"',
                # 应被忽略：Windows / macOS / extra
                'pywin32>=300; platform_system == "Windows"',
                'pyobjc; platform_system == "Darwin"',
                'cuda-toolkit; extra == "cuda"',
            ],
        )
        make_wheel(
            d,
            "cuda_python-12.9.4-py3-none-any.whl",
            ["cuda-bindings~=12.9.4"],  # 无 marker 的传递依赖
        )
        # numpy / httpx 已存在（避免误报）
        make_wheel(d, "numpy-2.3.5-cp312-cp312-manylinux_2_28_x86_64.whl", [])
        make_wheel(d, "httpx-0.28.1-py3-none-any.whl", [])

        missing = gpu_deps.collect(d)
        names = {gpu_deps.requirement_name(m) for m in missing}
        print("缺失:", sorted(names))
        check("识别 Linux 标记依赖（cudnn）", "nvidia_cudnn_cu12" in names, str(names))
        check("识别 Linux 标记依赖（nvrtc）", "nvidia_cuda_nvrtc_cu12" in names, str(names))
        check("识别无标记传递依赖（cuda-bindings）", "cuda_bindings" in names, str(names))
        check("不误报 Windows 专属", "pywin32" not in names, str(names))
        check("不误报 macOS 专属", "pyobjc" not in names, str(names))
        check("不误报 extras", "cuda_toolkit" not in names, str(names))
        check("不误报已存在依赖", "numpy" not in names and "httpx" not in names, str(names))

        # 补上缺失的包后应为空
        make_wheel(d, "nvidia_cudnn_cu12-9.9.0.52-py3-none-manylinux_2_25_x86_64.whl", [])
        make_wheel(d, "nvidia_cuda_nvrtc_cu12-12.9.41-py3-none-manylinux_2_25_x86_64.whl", [])
        make_wheel(d, "cuda_bindings-12.9.4-cp312-cp312-manylinux_2_28_x86_64.whl", [])
        left = gpu_deps.collect(d)
        check("补齐后无缺失", left == set(), str(left))

        # 要求版本不一致时仍应报告（防止"装了别的版本"被误判为齐全）
        d2 = d / "ver"
        d2.mkdir()
        make_wheel(
            d2,
            "paddlepaddle_gpu-3.4.0-cp312-cp312-linux_x86_64.whl",
            ['nvidia-cudnn-cu12==9.9.0.52; platform_system == "Linux"'],
        )
        make_wheel(d2, "nvidia_cudnn_cu12-9.8.0.87-py3-none-manylinux_2_25_x86_64.whl", [])
        check("版本不符仍报缺失", len(gpu_deps.collect(d2)) == 1, str(gpu_deps.collect(d2)))

        # ~=1.1 应接受 1.8.x（PEP 440：>=1.1, ==1.*），不能只匹配 1.1.*
        d3 = d / "compat"
        d3.mkdir()
        make_wheel(
            d3,
            "cuda_bindings-12.9.4-cp312-cp312-manylinux_2_28_x86_64.whl",
            ["cuda-pathfinder~=1.1"],
        )
        make_wheel(d3, "cuda_pathfinder-1.8.4-py3-none-any.whl", [])
        check("~=1.1 接受 1.8.4", gpu_deps.collect(d3) == set(), str(gpu_deps.collect(d3)))

        # 目标固定 linux/x86_64：aarch64/arm64 标记的依赖装不上，不能算"缺失"
        d4 = d / "arch"
        d4.mkdir()
        make_wheel(
            d4,
            "paddlepaddle_gpu-3.4.0-cp312-cp312-linux_x86_64.whl",
            [
                'nvidia-cudnn-cu12==9.9.0.52; platform_system == "Linux" and platform_machine == "x86_64"',
                'some-arm-helper; platform_machine == "aarch64"',
                'another-arm; platform_machine == "arm64"',
            ],
        )
        names4 = {gpu_deps.requirement_name(m) for m in gpu_deps.collect(d4)}
        check("不误报 aarch64 专属", "some_arm_helper" not in names4, str(names4))
        check("不误报 arm64 专属", "another_arm" not in names4, str(names4))
        check("仍报 x86_64 缺失", "nvidia_cudnn_cu12" in names4, str(names4))

        # python_version 条件必须按【目标解释器】判断，而不是下载机的解释器。
        # 真实案例：paddlepaddle-gpu 有 `setuptools; python_version >= "3.12"`，
        # 在 python3.10 的下载机上会被 pip 跳过，到 cp312 目标机才暴露缺包。
        d5 = d / "pymarker"
        d5.mkdir()
        make_wheel(
            d5,
            "paddlepaddle_gpu-3.4.0-cp312-cp312-linux_x86_64.whl",
            [
                'setuptools; python_version >= "3.12"',
                'tomli>=1.1.0; python_version < "3.11"',
                'numpy>=2.3.3; python_version >= "3.14"',
                'typing_extensions>=4.10.0; python_version < "3.13"',
            ],
        )
        names5 = {gpu_deps.requirement_name(m) for m in gpu_deps.collect(d5, "3.12")}
        check("目标 3.12：保留 setuptools（>= 3.12 成立）", "setuptools" in names5, str(names5))
        check("目标 3.12：丢弃 tomli（< 3.11 不成立）", "tomli" not in names5, str(names5))
        check("目标 3.12：丢弃 numpy 3.14 分支", "numpy" not in names5, str(names5))
        check(
            "目标 3.12：保留 typing_extensions（< 3.13 成立）",
            "typing_extensions" in names5,
            str(names5),
        )
        names514 = {gpu_deps.requirement_name(m) for m in gpu_deps.collect(d5, "3.14")}
        check("目标 3.14：保留 numpy>=2.3.3（3.14 分支）", "numpy" in names514, str(names514))
        check("目标 3.14：丢弃 typing_extensions（< 3.13 不成立）", "typing_extensions" not in names514, str(names514))
        # 版本比较本身
        check("_cmp_ok 3.12 >= 3.12", gpu_deps._cmp_ok(">=", (3, 12), "3.12"))
        check("_cmp_ok 3.12 < 3.13", gpu_deps._cmp_ok("<", (3, 12), "3.13"))
        check("_cmp_ok 3.12.10 >= 3.12", gpu_deps._cmp_ok(">=", (3, 12, 10), "3.12"))
        check("_cmp_ok ~=3.12 接受 3.12", gpu_deps._cmp_ok("~=", (3, 12), "3.12"))
        check("_cmp_ok ~=3.12 拒绝 4.0", not gpu_deps._cmp_ok("~=", (4, 0), "3.12"))

        # sys_platform 非 linux（emscripten 等）的依赖不能算缺失（httpx2-jsfetch 真实案例）
        d6 = d / "emark"
        d6.mkdir()
        make_wheel(
            d6,
            "httpx2-1.0-py3-none-any.whl",
            [
                "httpx2-jsfetch; sys_platform == 'emscripten' and python_version >= '3.12'",
                "linux-only-helper; sys_platform == 'linux'",
            ],
        )
        names6 = {gpu_deps.requirement_name(m) for m in gpu_deps.collect(d6, "3.12")}
        check("不误报 emscripten 专属", "httpx2_jsfetch" not in names6, str(names6))
        check("保留 sys_platform==linux 依赖", "linux_only_helper" in names6, str(names6))

    print()
    if FAILED:
        print(f"失败 {FAILED} 项")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
