#!/usr/bin/env python3
"""把 Indra OCR 模型下载到本地目录，供离线部署使用。

用法（在有外网的机器上执行）：
    python3 scripts/download_indra_models.py
    INDRA_OCR_MODEL_DIR=models/indra python3 scripts/download_indra_models.py
    python3 scripts/download_indra_models.py --math     # 额外下载公式识别模型

下载完成后把 models/indra 随项目拷到离线服务器，并在 config.yaml 设置：
    indra:
      model_dir: models/indra

说明：该脚本不导入 backend.settings，因此不会设置 HF_HUB_OFFLINE，
首次运行允许下载权重。

国内网络可用 HF_ENDPOINT 指定镜像（huggingface_hub 原生支持）：
    HF_ENDPOINT=https://hf-mirror.com python3 scripts/download_indra_models.py
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIR = PROJECT_ROOT / "models" / "indra"


def main() -> int:
    parser = argparse.ArgumentParser(description="下载 Indra OCR 权重到本地目录（供离线部署）")
    parser.add_argument("--math", action="store_true",
                        help="同时下载公式识别模型（对应 config.yaml 的 indra.math: true）")
    args = parser.parse_args()

    try:
        import indra_ocr
        from indra_ocr import models as indra_models
    except Exception as exc:  # noqa: BLE001
        print("[失败] 未安装 indra-ocr。请先执行：pip install -r requirements-indra.txt")
        print(f"       原始错误：{exc}")
        return 2

    target = Path(os.environ.get("INDRA_OCR_MODEL_DIR") or DEFAULT_DIR).expanduser()
    if not target.is_absolute():
        target = (PROJECT_ROOT / target).resolve()
    target.mkdir(parents=True, exist_ok=True)

    # 必须自己调 snapshot_download：indra_ocr.load(path=...) 只检查目标目录里
    # 有没有权重，缺文件直接抛 ModelError，它永远不会往 path 里下载。
    from huggingface_hub import snapshot_download

    repo = indra_models.HF_REPO
    revision = indra_models.HF_REVISION
    endpoint = os.environ.get("HF_ENDPOINT", "").strip()
    allow = ["*.onnx", "meta.json", "lm.npz", "lm_*.npz"]
    ignore = list(indra_models.MATH_FILES)
    if args.math:
        allow += list(indra_models.MATH_FILES)
        ignore = []
    print(f"下载 Indra OCR 模型到：{target}")
    print(f"  来源：Hugging Face {repo}@{revision}" + (f"（镜像 {endpoint}）" if endpoint else ""))
    if args.math:
        print("  含公式识别模型（--math）")
    try:
        snapshot_download(
            repo_id=repo,
            revision=revision,
            local_dir=str(target),
            allow_patterns=allow,
            ignore_patterns=ignore,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[失败] 模型下载失败：{exc}")
        print("       国内网络可加镜像重试：HF_ENDPOINT=https://hf-mirror.com " + " ".join(sys.argv))
        return 1

    # 校验：目录里必须有 indra-ocr 运行时要求的全部权重
    try:
        indra_ocr.load(path=str(target), math=args.math)
    except Exception as exc:  # noqa: BLE001
        print(f"[失败] 模型校验失败：{exc}")
        return 1

    size = sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
    files = sum(1 for f in target.rglob("*") if f.is_file())
    print(f"[完成] {files} 个文件，合计 {size / 1024 / 1024:.1f} MB")
    print("下一步：把 models/indra 拷贝到离线服务器，并在 config.yaml 设置 indra.model_dir: models/indra")
    return 0


if __name__ == "__main__":
    sys.exit(main())
