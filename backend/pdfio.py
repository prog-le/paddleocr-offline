"""输入文件处理：图片与 PDF 逐页渲染为可直接送入 PaddleX 的图像文件。

为什么自己渲染 PDF 而不用 PaddleX 内置的 PDF 支持：
  1. 进程内逐页处理 -> 可精确回报进度、支持取消、控制峰值内存；
  2. 可控 DPI / 长边 / 像素上限，保护小内存 ARM 服务器；
  3. 页面图落盘，推理报错时可复现排查。

渲染后端按可用性依次降级（三者均为纯 pip 依赖，双架构 wheel 齐全，无需外网）：
  PyMuPDF(fitz)  ->  pypdfium2  ->  pdf2image+poppler
"""

from __future__ import annotations

import io
import os
from pathlib import Path
from typing import List, Optional, Tuple

from PIL import Image, ImageOps

Image.MAX_IMAGE_PIXELS = None  # 自己控制上限，避免 PIL 直接抛 DecompressionBombError

PDF_SUFFIX = {".pdf"}
IMAGE_SUFFIX = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}
SUPPORTED_SUFFIX = PDF_SUFFIX | IMAGE_SUFFIX


class InputError(Exception):
    """输入文件不可处理（损坏、加密、格式不支持）。"""


class PageLimitError(InputError):
    """页数超过配置上限。"""


# --------------------------------------------------------------------------
# 渲染后端探测
# --------------------------------------------------------------------------
def _import_pymupdf():  # noqa: ANN202
    """PyMuPDF 2.x 推荐 `import pymupdf`，1.x 只有 `import fitz`，两者都兼容。"""
    try:
        import pymupdf  # type: ignore

        return pymupdf
    except Exception:
        pass
    try:
        import fitz  # type: ignore

        return fitz
    except Exception:
        return None


def _detect_pdf_backend() -> str:
    if _import_pymupdf() is not None:
        return "pymupdf"
    try:
        import pypdfium2  # noqa: F401

        return "pypdfium2"
    except Exception:
        pass
    from shutil import which

    if which("pdftoppm"):
        return "pdf2image"
    raise InputError(
        "未找到可用的 PDF 渲染后端：请安装 PyMuPDF 或 pypdfium2（pip）或 poppler-utils（系统包）"
    )


def pdf_backend_name() -> str:
    return _detect_pdf_backend()


def probe(path: Path) -> Tuple[str, int]:
    """返回 (kind, page_count)。kind 为 'pdf' 或 'image'。"""
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIX:
        raise InputError(f"不支持的文件类型：{suffix or '(无扩展名)'}，仅支持 {sorted(SUPPORTED_SUFFIX)}")
    if suffix in PDF_SUFFIX:
        return "pdf", pdf_page_count(path)
    return "image", image_frame_count(path)


def pdf_page_count(path: Path) -> int:
    backend = _detect_pdf_backend()
    try:
        if backend == "pymupdf":
            fitz = _import_pymupdf()

            with fitz.open(str(path)) as doc:
                if doc.needs_pass:
                    raise InputError("PDF 已加密，无法解析，请先解除密码保护")
                return doc.page_count
        if backend == "pypdfium2":
            import pypdfium2 as pdfium

            doc = pdfium.PdfDocument(str(path))
            try:
                return len(doc)
            finally:
                doc.close()
        from pdf2image import pdfinfo_from_path

        info = pdfinfo_from_path(str(path))
        return int(info.get("Pages") or 0)
    except InputError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise InputError(f"PDF 解析失败：{exc}") from exc


def image_frame_count(path: Path) -> int:
    try:
        with Image.open(path) as img:
            return int(getattr(img, "n_frames", 1) or 1)
    except Exception as exc:  # noqa: BLE001
        raise InputError(f"图片解析失败：{exc}") from exc


# --------------------------------------------------------------------------
# 图像规整
# --------------------------------------------------------------------------
def _limit_size(width: int, height: int, max_side: int, max_pixels: int) -> Tuple[int, int]:
    scale = 1.0
    if max_side and max(width, height) > max_side:
        scale = min(scale, max_side / float(max(width, height)))
    if max_pixels and width * height * scale * scale > max_pixels:
        import math

        scale = min(scale, math.sqrt(max_pixels / float(width * height)))
    if scale >= 0.999:
        return width, height
    return max(int(width * scale), 1), max(int(height * scale), 1)


def _save_image(img: Image.Image, out_path: Path, quality: int = 94) -> Tuple[int, int]:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    suffix = out_path.suffix.lower()
    if suffix in (".jpg", ".jpeg"):
        img.save(out_path, format="JPEG", quality=quality, optimize=False, subsampling=1)
    else:
        img.save(out_path, format="PNG", optimize=False)
    return img.width, img.height


def render_page(
    path: Path,
    kind: str,
    index: int,
    out_path: Path,
    dpi: int = 200,
    max_side: int = 2400,
    max_pixels: int = 50_000_000,
) -> Tuple[int, int]:
    """把第 index 页（0 基）渲染/规整为 out_path，返回 (width, height)。"""
    if kind == "pdf":
        img = _render_pdf_page(path, index, dpi)
    else:
        img = _load_image_frame(path, index, max_pixels)

    try:
        new_w, new_h = _limit_size(img.width, img.height, max_side, max_pixels)
        if (new_w, new_h) != (img.width, img.height):
            img = img.resize((new_w, new_h), Image.LANCZOS)
        return _save_image(img, out_path)
    finally:
        try:
            img.close()
        except Exception:
            pass


def _render_pdf_page(path: Path, index: int, dpi: int) -> Image.Image:
    backend = _detect_pdf_backend()
    try:
        if backend == "pymupdf":
            fitz = _import_pymupdf()

            with fitz.open(str(path)) as doc:
                page = doc.load_page(index)
                pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csRGB, alpha=False)
                return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)

        if backend == "pypdfium2":
            import pypdfium2 as pdfium

            doc = pdfium.PdfDocument(str(path))
            try:
                page = doc[index]
                bitmap = page.render(scale=dpi / 72.0)
                return _pdfium_bitmap_to_pil(bitmap)
            finally:
                doc.close()

        from pdf2image import convert_from_path

        images = convert_from_path(
            str(path), dpi=dpi, first_page=index + 1, last_page=index + 1, fmt="png"
        )
        if not images:
            raise InputError(f"PDF 第 {index + 1} 页渲染结果为空")
        return images[0]
    except InputError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise InputError(f"PDF 第 {index + 1} 页渲染失败（后端 {backend}）：{exc}") from exc


def _pdfium_bitmap_to_pil(bitmap) -> Image.Image:
    """兼容 pypdfium2 v4/v5 的不同转 PIL 接口。"""
    for attr in ("to_pil", "to_pil_image"):
        fn = getattr(bitmap, attr, None)
        if callable(fn):
            return fn()
    to_numpy = getattr(bitmap, "to_numpy", None)
    if callable(to_numpy):
        return Image.fromarray(to_numpy())
    buffer = getattr(bitmap, "buffer", None) or getattr(bitmap, "memoryview", None)
    if buffer is not None:
        mode = "RGBA" if getattr(bitmap, "mode", "") == "BGRA" else "RGB"
        return Image.frombuffer(mode, (bitmap.width, bitmap.height), bytes(buffer), "raw", mode, 0, 1)
    raise InputError("pypdfium2 位图转换失败（版本不兼容）")


def _load_image_frame(path: Path, index: int, max_pixels: int = 0) -> Image.Image:
    try:
        with Image.open(path) as img:
            if index:
                img.seek(index)
            # 解码前先看尺寸，避免超大图直接把内存打满（解压炸弹）
            if max_pixels and img.width * img.height > max_pixels * 4:
                raise InputError(
                    f"图片分辨率过大（{img.width}x{img.height}），"
                    f"超过上限 {max_pixels} 像素的 4 倍，请先压缩后再上传"
                )
            has_alpha = img.mode in ("RGBA", "LA") or (
                img.mode == "P" and "transparency" in img.info
            )
            img.load()
            img = ImageOps.exif_transpose(img)
            if has_alpha or img.mode == "P":
                rgba = img.convert("RGBA")
                background = Image.new("RGB", rgba.size, (255, 255, 255))
                background.paste(rgba, mask=rgba.split()[-1])
                return background
            return img.convert("RGB")
    except InputError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise InputError(f"图片读取失败：{exc}") from exc


# --------------------------------------------------------------------------
# 上传文件校验
# --------------------------------------------------------------------------
PDF_MAGIC = b"%PDF-"
IMAGE_MAGIC = (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"BM", b"II*\x00", b"MM\x00*", b"RIFF")


def sniff_kind(path: Path) -> Optional[str]:
    """按文件头判断真实类型，避免扩展名伪装。"""
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except OSError:
        return None
    if head.startswith(PDF_MAGIC):
        return "pdf"
    for magic in IMAGE_MAGIC:
        if head.startswith(magic):
            return "image"
    return None
