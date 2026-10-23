"""统一的文档中间模型（IR）。

PaddleX 不同产线的输出结构不同（PP-StructureV3 有 parsing_res_list，
OCR 产线只有 rec_texts/rec_polys），本模块把它们归一化成：

    Doc -> pages[] -> blocks[] (type: title/text/table/figure/caption/formula/header/footer)

四种导出（docx/xlsx/txt/md）与前端预览都只消费这一层，保证行为一致。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from .tableparse import Table

# PaddleX 版面标签 -> 归一化类型
LABEL_TYPE_MAP = {
    "doc_title": "title",
    "paragraph_title": "title",
    "title": "title",
    "section_title": "title",
    "text": "text",
    "abstract": "text",
    "content": "text",
    "reference": "text",
    "list": "text",
    "vertical_text": "text",
    "table": "table",
    "table_title": "caption",
    "figure_title": "caption",
    "chart_title": "caption",
    "image": "figure",
    "figure": "figure",
    "chart": "figure",
    "header": "header",
    "header_image": "header",
    "footer": "footer",
    "footer_image": "footer",
    "number": "noise",
    "footnote": "noise",
    "aside_text": "noise",
    "formula": "formula",
    "seal": "figure",
    "unknown": "text",
}

NOISE_TYPES = {"header", "footer", "noise"}


def normalize_label(label: str) -> str:
    key = (label or "").strip().lower()
    return LABEL_TYPE_MAP.get(key, "text")


@dataclass
class Block:
    type: str = "text"
    label: str = "text"
    bbox: List[float] = field(default_factory=lambda: [0, 0, 0, 0])
    text: str = ""
    table: Optional[Table] = None
    score: float = 0.0
    order: Optional[int] = None

    @property
    def is_noise(self) -> bool:
        return self.type in NOISE_TYPES

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "type": self.type,
            "label": self.label,
            "bbox": [round(float(v), 1) for v in (self.bbox or [])][:4],
            "text": self.text,
            "score": round(float(self.score or 0), 4),
            "order": self.order,
        }
        if self.table is not None:
            d["table"] = self.table.to_dict()
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Block":
        table = Table.from_dict(d["table"]) if isinstance(d.get("table"), dict) else None
        return cls(
            type=str(d.get("type") or "text"),
            label=str(d.get("label") or "text"),
            bbox=[float(v) for v in (d.get("bbox") or [0, 0, 0, 0])][:4],
            text=str(d.get("text") or ""),
            table=table,
            score=float(d.get("score") or 0),
            order=d.get("order"),
        )


@dataclass
class Page:
    index: int = 1
    width: int = 0
    height: int = 0
    blocks: List[Block] = field(default_factory=list)
    elapsed_ms: int = 0

    @property
    def tables(self) -> List[Block]:
        return [b for b in self.blocks if b.type == "table" and b.table is not None]

    def visible_blocks(self, include_noise: bool = False) -> List[Block]:
        return [b for b in self.blocks if include_noise or not b.is_noise]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "width": self.width,
            "height": self.height,
            "elapsed_ms": self.elapsed_ms,
            "blocks": [b.to_dict() for b in self.blocks],
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Page":
        return cls(
            index=int(d.get("index") or 1),
            width=int(d.get("width") or 0),
            height=int(d.get("height") or 0),
            elapsed_ms=int(d.get("elapsed_ms") or 0),
            blocks=[Block.from_dict(b) for b in (d.get("blocks") or []) if isinstance(b, dict)],
        )


@dataclass
class Doc:
    name: str = ""
    source_type: str = "image"  # image | pdf
    pipeline: str = ""
    pages: List[Page] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    params: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    # -------------------------------------------------------------- 统计信息
    def stats(self) -> Dict[str, int]:
        tables = 0
        chars = 0
        blocks = 0
        for page in self.pages:
            for b in page.blocks:
                if b.is_noise:
                    continue
                blocks += 1
                if b.type == "table" and b.table is not None:
                    tables += 1
                    chars += sum(len(c.text) for c in b.table.cells)
                else:
                    chars += len(b.text or "")
        return {"pages": len(self.pages), "blocks": blocks, "tables": tables, "chars": chars}

    def iter_tables(self) -> Iterable[Block]:
        for page in self.pages:
            for b in page.blocks:
                if b.type == "table" and b.table is not None and not b.table.is_empty:
                    yield b

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "source_type": self.source_type,
            "pipeline": self.pipeline,
            "created_at": self.created_at,
            "params": self.params,
            "warnings": self.warnings,
            "stats": self.stats(),
            "pages": [p.to_dict() for p in self.pages],
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Doc":
        return cls(
            name=str(d.get("name") or ""),
            source_type=str(d.get("source_type") or "image"),
            pipeline=str(d.get("pipeline") or ""),
            created_at=float(d.get("created_at") or time.time()),
            params=d.get("params") or {},
            warnings=list(d.get("warnings") or []),
            pages=[Page.from_dict(p) for p in (d.get("pages") or []) if isinstance(p, dict)],
        )
