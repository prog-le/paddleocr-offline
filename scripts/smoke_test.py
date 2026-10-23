#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线自检：验证「模型可加载 + 可推理 + 四种导出可用」。

在目标服务器（无外网）执行：
    .venv/bin/python scripts/smoke_test.py
    .venv/bin/python scripts/smoke_test.py --image /path/to/test.png
    .venv/bin/python scripts/smoke_test.py --mock          # 只测 Web/导出链路，不加载模型

退出码 0 表示全部通过。
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def gen_sample(path: Path) -> Path:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (1000, 900), "white")
    d = ImageDraw.Draw(img)
    d.text((60, 50), "PaddleX OCR smoke test / 离线自检", fill="black")
    d.text((60, 100), "If you can read this line, OCR works.", fill="black")
    top, left = 200, 60
    for i in range(4):
        d.line([(left, top + i * 70), (left + 800, top + i * 70)], fill="black", width=2)
    for i in range(3):
        d.line([(left + i * 260, top), (left + i * 260, top + 210)], fill="black", width=2)
    d.line([(left + 780, top), (left + 780, top + 210)], fill="black", width=2)
    cells = [["姓名", "部门", "工号"], ["张三", "研发部", "A001"], ["李四", "财务部", "B002"]]
    for r, row in enumerate(cells):
        for c, text in enumerate(row):
            d.text((left + c * 260 + 16, top + r * 70 + 22), text, fill="black")
    img.save(path)
    return path


def _run_one(spec, settings, args, image: Path, tmpdir: Path) -> list[str]:
    """对单条产线跑：加载 -> 推理 -> 四种导出 -> docx/xlsx 回读。"""
    failures: list[str] = []
    label = f"{spec.label}（{spec.id}）" if spec is not None else "默认产线"
    print("-" * 66)
    print(f" 产线：{label}  [kind={getattr(spec, 'kind', '?')}]")
    print("-" * 66)

    started = time.time()
    if args.mock:
        from backend.mock_engine import MockEngine

        engine = MockEngine(settings, spec)
        print("[1/4] MOCK 引擎就绪")
    else:
        from backend.engine import PaddleXEngine

        engine = PaddleXEngine(settings, spec)
        try:
            engine.ensure_loaded()
        except Exception as exc:  # noqa: BLE001
            print(f"[1/4] 产线加载失败：{exc}")
            return [f"{label} 加载失败"]
        print(f"[1/4] 产线加载成功，用时 {time.time() - started:.1f}s")
        inventory = engine.model_inventory()
        print(f"      模型数量 {len(inventory['models'])}，目录 {inventory['dir']}")
        if not inventory["models"]:
            failures.append("模型目录为空（模型未随包分发）")

    try:
        result = engine.predict_page(image, {})
        tables = [b for b in result.blocks if b.type == "table" and b.table is not None]
        print(f"[2/4] 推理成功：{len(result.blocks)} 个块，表格 {len(tables)} 个，{result.elapsed_ms} ms")
        if not result.blocks:
            failures.append(f"{label} 推理结果为空")
        if getattr(spec, "kind", "") == "table" and not tables:
            # 表格产线在无表格页面会退回文本，这里只提示不判失败
            print("      提示：本页未检出表格（已退回 OCR 文本）")
    except Exception as exc:  # noqa: BLE001
        print(f"[2/4] 推理失败：{exc}")
        return failures + [f"{label} 推理失败"]

    # 3) 组装文档并导出四种格式
    from backend.converters import export_document
    from backend.docmodel import Doc, Page

    doc = Doc(
        name="smoke-test",
        source_type="image",
        pipeline=getattr(engine, "label", None) or getattr(engine, "pipeline_name", "?"),
    )
    doc.pages.append(Page(index=1, width=result.width, height=result.height, blocks=result.blocks))

    outputs = {}
    for fmt in ("txt", "md", "docx", "xlsx"):
        out = tmpdir / f"smoke_{spec.id if spec else 'default'}.{fmt}"
        try:
            export_document(doc, fmt, out)
            size = out.stat().st_size
            outputs[fmt] = out
            print(f"[3/4] 导出 {fmt:<4} 成功（{size} 字节）")
            if size < 32:
                failures.append(f"{fmt} 导出内容过小")
        except Exception as exc:  # noqa: BLE001
            print(f"[3/4] 导出 {fmt} 失败：{exc}")
            failures.append(f"{fmt} 导出失败")

    # 4) 回读校验（docx/xlsx 结构可解析）
    try:
        from docx import Document
        from openpyxl import load_workbook

        if "docx" in outputs:
            Document(str(outputs["docx"]))
        sheets = []
        if "xlsx" in outputs:
            sheets = load_workbook(str(outputs["xlsx"])).sheetnames
        print(f"[4/4] 回读成功：docx 可打开，xlsx 工作表 {sheets}")
    except Exception as exc:  # noqa: BLE001
        print(f"[4/4] 回读失败：{exc}")
        failures.append("导出的 docx/xlsx 无法回读")

    try:
        if hasattr(engine, "close"):
            engine.close()
    except Exception:  # noqa: BLE001
        pass
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default="", help="测试图片（默认自动生成一张带表格的样张）")
    parser.add_argument("--mock", action="store_true", help="只测 Web/导出链路，不加载模型")
    parser.add_argument("--pipeline", default=os.environ.get("PADDLEX_OCR_PIPELINE", ""),
                        help="产线 id（如 OCR / PP-StructureV3 / table_recognition_v2），留空用默认产线")
    parser.add_argument("--all", action="store_true", help="依次自检全部产线（模型会逐条加载/释放，耗时较长）")
    args = parser.parse_args()

    from backend.pipelines import build_registry, find_spec
    from backend.settings import Settings, apply_paddlex_env

    settings = Settings()
    apply_paddlex_env(settings)
    specs = build_registry(settings)

    if args.all:
        selected = list(specs)
    elif args.pipeline:
        spec = find_spec(specs, args.pipeline)
        if spec is None:
            print(f"[失败] 未知产线：{args.pipeline}")
            print("       可选：" + "、".join(f"{sp.id}（{sp.label}）" for sp in specs))
            return 2
        selected = [spec]
    else:
        default_id = specs[0].id if specs else ""
        try:
            from backend.pipelines import resolve_default_id

            default_id = resolve_default_id(settings, specs)
        except Exception:  # noqa: BLE001
            pass
        selected = [find_spec(specs, default_id) or (specs[0] if specs else None)]

    selected = [sp for sp in selected if sp is not None]
    if not selected:
        print("[失败] 没有可用产线（config/pipelines 为空？）")
        return 2

    print("=" * 66)
    print(" 离线自检开始")
    print(f"  项目目录 : {PROJECT_ROOT}")
    print(f"  模型目录 : {settings.official_models_dir}")
    print(f"  自检产线 : {', '.join(sp.id for sp in selected)}")
    try:
        from backend import devices as _devices

        print(f"  运行设备 : {_devices.describe(settings)}")
    except Exception:  # noqa: BLE001
        pass
    print("=" * 66)

    tmpdir = Path(tempfile.mkdtemp(prefix="ocr-smoke-"))
    image = Path(args.image) if args.image else gen_sample(tmpdir / "sample.png")
    if not image.exists():
        print(f"[失败] 测试图片不存在：{image}")
        return 2

    failures: list[str] = []
    for spec in selected:
        failures += _run_one(spec, settings, args, image, tmpdir)

    print("=" * 66)
    if failures:
        print("自检未通过：")
        for item in failures:
            print("  - " + item)
        print(f"临时文件保留在：{tmpdir}")
        return 1
    print(f"自检全部通过（{len(selected)} 条产线），服务可正常识别与导出。")
    print(f"（临时文件：{tmpdir}，可删除）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
