#!/usr/bin/env python3
"""把 Indra OCR 模型下载到本地目录，供离线部署使用。

用法（在有外网的机器上执行）：
    python3 scripts/download_indra_models.py
    INDRA_OCR_MODEL_DIR=models/indra python3 scripts/download_indra_models.py

下载完成后把 models/indra 随项目拷到离线服务器，并在 config.yaml 设置：
    indra:
      model_dir: models/indra

说明：该脚本不导入 backend.settings，因此不会设置 HF_HUB_OFFLINE，
首次运行允许 indra-ocr 自行下载权重。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIR = PROJECT_ROOT / "models" / "indra"


def main() -> int:
    try:
        import indra_ocr
    except Exception as exc:  # noqa: BLE001
        print("[失败] 未安装 indra-ocr。请先执行：pip install -r requirements-indra.txt")
        print(f"       原始错误：{exc}")
        return 2

    target = Path(os.environ.get("INDRA_OCR_MODEL_DIR") or DEFAULT_DIR).expanduser()
    if not target.is_absolute():
        target = (PROJECT_ROOT / target).resolve()
    target.mkdir(parents=True, exist_ok=True)

    print(f"下载 Indra OCR 模型到：{target}")
    try:
        indra_ocr.load(path=str(target))
    except Exception as exc:  # noqa: BLE001
        print(f"[失败] 模型下载失败：{exc}")
        return 1

    size = sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
    files = sum(1 for f in target.rglob("*") if f.is_file())
    print(f"[完成] {files} 个文件，合计 {size / 1024 / 1024:.1f} MB")
    print("下一步：把 models/indra 拷贝到离线服务器，并在 config.yaml 设置 indra.model_dir: models/indra")
    return 0


if __name__ == "__main__":
    sys.exit(main())
