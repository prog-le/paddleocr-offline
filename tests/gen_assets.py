#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成端到端测试用的图片与多页 PDF（不依赖 reportlab，用 Pillow 直接写 PDF）。"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw


def make_page(no: int) -> Image.Image:
    img = Image.new("RGB", (1240, 1754), "white")  # A4 @150dpi
    d = ImageDraw.Draw(img)
    d.text((80, 70), f"PaddleX OCR e2e test - page {no}", fill="black")
    d.text((80, 110), "离线识别自检 第 %d 页：文字 + 表格" % no, fill="black")
    for i in range(3):
        d.text((80, 160 + i * 34), f"Line {i + 1}: The quick brown fox jumps over the lazy dog.", fill="black")

    left, top, w, h = 80, 320, 300, 70
    for r in range(4):
        d.line([(left, top + r * h), (left + 3 * w, top + r * h)], fill="black", width=2)
    for c in range(4):
        d.line([(left + c * w, top), (left + c * w, top + 3 * h)], fill="black", width=2)
    cells = [["序号", "项目", "数量"], ["1", "打印纸", "10"], ["2", "硒鼓", "2"]]
    for r, row in enumerate(cells):
        for c, text in enumerate(row):
            d.text((left + c * w + 14, top + r * h + 24), text, fill="black")
    return img


def main(out_dir: str) -> int:
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    pages = [make_page(i + 1) for i in range(2)]
    pages[0].save(target / "sample.png")
    pages[0].save(target / "multi.pdf", save_all=True, append_images=pages[1:], resolution=150)
    print(f"generated: {target/'sample.png'} {target/'multi.pdf'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "tmp"))
