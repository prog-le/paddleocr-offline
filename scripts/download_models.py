#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模型离线打包脚本（在【有外网】的机器上执行一次）。

原理：
  PaddleX 首次创建产线时会从官方源下载模型权重到
  ``$PADDLE_PDX_CACHE_HOME/official_models/<模型名>/``。
  本脚本把缓存目录指向项目内 ``models/paddlex_home``，依次实例化目标产线并跑一次
  真实推理，触发全部模型落盘，最后写出 manifest.json 便于离线服务器核对。

用法：
    python3 scripts/download_models.py                       # 默认：config/pipelines 下全部产线 + OCR
    python3 scripts/download_models.py --pipelines PP-StructureV3
    python3 scripts/download_models.py --out models/paddlex_home --keep-cache

离线服务器上无需执行本脚本，直接把整个 models/ 目录随项目拷贝即可。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_PIPELINES = ["PP-StructureV3", "OCR"]  # 仅供说明/兼容，实际默认值见 --pipelines 的帮助


def gen_sample_image(path: Path) -> Path:
    """生成一张带中英文与表格线的样张，用于触发全部子模型下载与自检。"""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (1200, 1500), "white")
    draw = ImageDraw.Draw(img)
    draw.text((80, 70), "PaddleX OCR offline packaging sample", fill="black")
    draw.text((80, 120), "离线模型打包样张 2026", fill="black")
    draw.text((80, 180), "The quick brown fox jumps over the lazy dog.", fill="black")
    draw.text((80, 220), "文字识别、表格识别与版面分析将被依次加载。", fill="black")

    top, left, right = 320, 80, 1120
    row_h, col_w = 60, 260
    for i in range(5):
        y = top + i * row_h
        draw.line([(left, y), (right, y)], fill="black", width=2)
    for j in range(4):
        x = left + j * col_w
        draw.line([(x, top), (x, top + 4 * row_h)], fill="black", width=2)
    draw.line([(right, top), (right, top + 4 * row_h)], fill="black", width=2)
    draw.line([(left, top + 4 * row_h), (right, top + 4 * row_h)], fill="black", width=2)

    headers = ["No.", "Item", "Qty", "Price"]
    for j, text in enumerate(headers):
        draw.text((left + j * col_w + 14, top + 18), text, fill="black")
    rows = [["1", "Printer paper", "10", "25.00"],
            ["2", "Toner", "2", "380.00"],
            ["3", "USB cable", "5", "18.50"]]
    for i, row in enumerate(rows):
        for j, text in enumerate(row):
            draw.text((left + j * col_w + 14, top + (i + 1) * row_h + 18), text, fill="black")

    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)
    return path


def dir_digest(path: Path) -> str:
    h = hashlib.sha256()
    for file in sorted(p for p in path.rglob("*") if p.is_file()):
        h.update(file.name.encode("utf-8", "ignore"))
        h.update(str(file.stat().st_size).encode())
    return h.hexdigest()[:16]


def main() -> int:
    parser = argparse.ArgumentParser(description="PaddleX 模型离线打包")
    parser.add_argument("--pipelines", default="",
                        help="要下载的 PaddleX 内置产线名，逗号分隔（如 PP-StructureV3,OCR）；"
                             "留空则下载 config/pipelines/*.yaml 全部产线 + OCR")
    parser.add_argument("--config", default="",
                        help="产线配置文件路径（如 config/pipelines/PP-StructureV3.yaml），"
                             "指定后忽略 --pipelines")
    parser.add_argument("--out", default=str(PROJECT_ROOT / "models" / "paddlex_home"),
                        help="模型缓存根目录（默认 models/paddlex_home）")
    parser.add_argument("--keep-cache", action="store_true",
                        help="保留运行期的 ~/.paddlex 缓存（默认结束后删除临时缓存）")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--manifest-only", action="store_true",
                        help="只扫描现有模型目录并重写 manifest.json（不下载、不推理）")
    args = parser.parse_args()

    out_root = Path(args.out).expanduser().resolve()
    official = out_root / "official_models"
    out_root.mkdir(parents=True, exist_ok=True)
    official.mkdir(parents=True, exist_ok=True)

    # 必须在 import paddlex 之前设置
    os.environ["PADDLE_PDX_CACHE_HOME"] = str(out_root)
    os.environ.setdefault("PADDLE_PDX_MODEL_SOURCE", "BOS")
    os.environ.pop("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", None)  # 下载阶段需要联网

    print("=" * 70)
    print(" PaddleX 模型离线打包")
    print(f" 缓存目录: {out_root}")
    print(f" 产线    : {args.pipelines}")
    print("=" * 70)

    try:
        import paddle
        import paddlex
    except Exception as exc:  # noqa: BLE001
        print(f"[失败] 无法导入 paddlex/paddle：{exc}")
        print("       请先在有外网的机器上 pip install -r requirements.txt")
        return 2

    print(f"paddle  : {paddle.__version__}")
    print(f"paddlex : {paddlex.__version__}")

    if args.manifest_only:
        print("\n[--manifest-only] 跳过下载与推理，只重建清单")
        targets = []
    else:
        sample = gen_sample_image(PROJECT_ROOT / "data" / "sample" / "model_probe.png")
    if not args.manifest_only and args.config:
        cfg_path = Path(args.config).expanduser()
        if not cfg_path.is_absolute():
            cfg_path = (PROJECT_ROOT / cfg_path).resolve()
        if not cfg_path.exists():
            print(f"[失败] 产线配置不存在：{cfg_path}")
            return 2
        targets = [(cfg_path.stem, str(cfg_path))]
        print(f"使用产线配置：{cfg_path}")
    elif not args.manifest_only and args.pipelines.strip():
        targets = [(name, name) for name in (p.strip() for p in args.pipelines.split(",")) if name]
    elif not args.manifest_only:
        # 默认：项目内置的全部产线 yaml + 内置 OCR，保证打包机不会漏模型
        pipelines_dir = PROJECT_ROOT / "config" / "pipelines"
        yamls = sorted(pipelines_dir.glob("*.yaml")) if pipelines_dir.is_dir() else []
        targets = [(y.stem, str(y)) for y in yamls]
        if ("OCR", "OCR") not in targets:
            targets.append(("OCR", "OCR"))
        print(f"未指定产线，按 config/pipelines 全量下载：{[t[0] for t in targets]}")

    failed: list[str] = []
    for name, pipeline_arg in targets:
        print(f"\n--- 下载产线模型：{name} ---")
        try:
            from paddlex import create_pipeline

            started = time.time()
            pipeline = create_pipeline(pipeline=pipeline_arg, device=args.device)
            print(f"    产线实例化完成（{time.time() - started:.1f}s），触发一次推理…")
            for res in pipeline.predict(input=str(sample)):
                # 只关心是否成功执行；结果不落盘
                _ = getattr(res, "json", None)
            print(f"    推理完成，用时 {time.time() - started:.1f}s")
        except Exception as exc:  # noqa: BLE001
            failed.append(f"{name}: {exc}")
            print(f"[失败] {name}: {exc}")

    # 汇总模型清单
    models = []
    for child in sorted(official.iterdir()):
        if not child.is_dir():
            continue
        size = sum(f.stat().st_size for f in child.rglob("*") if f.is_file())
        files = sum(1 for f in child.rglob("*") if f.is_file())
        models.append({
            "name": child.name,
            "size_mb": round(size / 1024 / 1024, 1),
            "files": files,
            "digest": dir_digest(child),
        })

    try:
        model_home_display = str(out_root.relative_to(PROJECT_ROOT))
    except ValueError:
        model_home_display = str(out_root)

    manifest = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "paddle": paddle.__version__,
        "paddlex": paddlex.__version__,
        "device": args.device,
        "pipelines": [name for name, _ in targets],
        "pipeline_config": args.config or None,
        "model_home": model_home_display,
        "model_count": len(models),
        "total_mb": round(sum(m["size_mb"] for m in models), 1),
        "models": models,
    }
    manifest_path = out_root.parent / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 70)
    print(f" 共 {len(models)} 个模型，合计 {manifest['total_mb']} MB")
    for m in models:
        print(f"   - {m['name']:<34} {m['size_mb']:>8} MB  ({m['files']} 文件)")
    print(f" 清单: {manifest_path}")
    print("=" * 70)

    if failed:
        print("\n以下产线未成功打包，请检查网络后重试：")
        for item in failed:
            print("  - " + item)
        return 1

    print("\n下一步：把 models/ 目录随项目一起打包（make_offline_bundle.sh 会自动包含）。")
    print("离线服务器无需联网，启动时 PADDLE_PDX_CACHE_HOME 会指向该目录。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
