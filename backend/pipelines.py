"""产线注册表。

把「内置 OCR」与 `config/pipelines/*.yaml`（PaddleX 产线配置）统一成前端可选列表，
每个产线一个稳定 id（= yaml 文件名 / `OCR`），任务创建时记录该 id，推理时路由到对应实例。

在 yaml 头部用注释声明展示信息（可选，缺省用文件名）：

    # label: 通用版面解析 v3（完整）
    # description: 含区域检测/公式/印章/图表，最慢，适合复杂版面
    # kind: structure          # 可选：ocr / structure / table，通常由 pipeline_name 自动判定

`kind` 决定「用哪个结果解析器」与「允许透传哪些参数」：
  * `ocr`       -> blocks_from_ocr()     ，参数集 OCR_PARAMS
  * `structure` -> blocks_from_structure()，参数集 STRUCTURE_PARAMS（PP-StructureV3 系）
  * `table`     -> blocks_from_table()   ，参数集 TABLE_PARAMS（table_recognition_v2）

展示顺序由 `PIPELINE_ORDER` 固定（纯文字 → 精简 → 完整 → 通用表格识别），
未登记的产线按 id 字母序排在末尾。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional

from .engine import OCR_PARAMS, STRUCTURE_PARAMS, TABLE_PARAMS
from .settings import PROJECT_ROOT, Settings
from .textio import read_text_file

log = logging.getLogger(__name__)

META_LINE = re.compile(r"^\s*#\s*(label|description)\s*[:：]\s*(.+?)\s*$")

# ---------------------------------------------------------------------------
# 配置文件的读取容错
# ---------------------------------------------------------------------------
# 线上踩过的坑：目标机上 `config/pipelines/` 里混入了非 UTF-8 文本
#   * macOS 拷来的 `._OCR.yaml`（AppleDouble，二进制）；
#   * 被编辑器存成 GBK 的 yaml（0xa3 之类的字节不是合法的 UTF-8 起始字节）。
# 旧实现 `open(..., encoding="utf-8")` 会抛 UnicodeDecodeError，而这个异常发生在
# `build_registry()` → import backend.main 阶段，导致整个服务起不来（uvicorn 直接退出）。
# 现在统一走 backend.textio：能解码就解码（UTF-8 优先，GB18030 兜底），
# 无法解码/是元数据文件就跳过并记日志，绝不因为一个杂项文件让进程崩掉。



DEFAULT_LABELS = {
    "PP-StructureV3": "通用版面解析 v3（精简）",
    "PP-StructureV3-full": "通用版面解析 v3（完整）",
    "OCR": "纯文字识别（PP-OCRv6）",
    "table_recognition_v2": "通用表格识别",
}
DEFAULT_DESCRIPTIONS = {
    "PP-StructureV3": "版面检测 + OCR + 表格结构，不加载公式/印章/图表，速度快、体积小",
    "PP-StructureV3-full": "官方全能力：区域检测 + 公式 + 印章 + 图表，最慢但最完整",
    "OCR": "PP-OCRv6 文本检测+识别，无表格结构，最快；档位 tiny/small/medium 由 ocr.ocr_tier 控制",
    "table_recognition_v2": "专用表格产线：表格分类 + 结构识别（有线/无线）+ 单元格检测，表格还原度最高，不解析正文段落",
}

# 前端下拉框的固定展示顺序（按需求：纯文字 → 精简 → 完整 → 通用表格识别）
PIPELINE_ORDER = [
    "OCR",
    "PP-StructureV3",
    "PP-StructureV3-full",
    "table_recognition_v2",
]

# PaddleX 产线名 -> 结果解析类型（决定用哪个 blocks 解析器与哪些可调参数）
KIND_BY_PIPELINE_NAME = {
    "OCR": "ocr",
    "table_recognition_v2": "table",
    "PP-StructureV3": "structure",
}


@dataclass(frozen=True)
class PipelineSpec:
    id: str
    label: str
    arg: str  # 传给 create_pipeline 的参数（yaml 路径或产线名）
    kind: str  # structure | ocr
    description: str = ""
    config_file: str = ""
    params: FrozenSet[str] = field(default_factory=frozenset)

    @property
    def is_structure(self) -> bool:
        return self.kind == "structure"

    def to_dict(self) -> Dict[str, str]:
        return {
            "id": self.id,
            "label": self.label,
            "kind": self.kind,
            "description": self.description,
            "config_file": self.config_file,
        }


def read_config_text(yaml_path: Path) -> Optional[str]:
    """读取产线配置文本；不可读/非文本返回 None（记日志，绝不抛异常）。

    这是 `build_registry()` 的唯一入口，保证"一个坏文件"不会让服务无法启动。
    """
    return read_text_file(yaml_path, what="产线配置")


def _read_meta(text: str) -> Dict[str, str]:
    """从 yaml 头部注释里取 label/description。"""
    meta: Dict[str, str] = {}
    for line in text.splitlines()[:40]:
        if not line.lstrip().startswith("#"):
            if line.strip():
                break  # 头部注释结束
            continue
        m = META_LINE.match(line)
        if m:
            meta[m.group(1)] = m.group(2)
    return meta


def _read_pipeline_name(text: str) -> str:
    """读 yaml 里的 pipeline_name（用于判定产线类型），失败则返回空串。"""
    try:
        import yaml as _yaml

        data = _yaml.safe_load(text) or {}
        if isinstance(data, dict):
            return str(data.get("pipeline_name") or "")
    except Exception:  # noqa: BLE001  配置读不出就用文件名兜底
        pass
    return ""


def _resolve_kind(stem: str, pipeline_name: str, meta: Dict[str, str]) -> str:
    """产线类型：ocr | structure | table。

    优先级：yaml 头部 `# kind: table` > pipeline_name 映射 > 文件名映射 > structure
    """
    explicit = (meta.get("kind") or "").strip().lower()
    if explicit in ("ocr", "structure", "table"):
        return explicit
    if pipeline_name in KIND_BY_PIPELINE_NAME:
        return KIND_BY_PIPELINE_NAME[pipeline_name]
    if stem in KIND_BY_PIPELINE_NAME:
        return KIND_BY_PIPELINE_NAME[stem]
    if pipeline_name.startswith("table_recognition") or stem.startswith("table_recognition"):
        return "table"
    if pipeline_name.upper() == "OCR":
        return "ocr"
    return "structure"


def _params_for_kind(kind: str) -> FrozenSet[str]:
    if kind == "ocr":
        return frozenset(OCR_PARAMS)
    if kind == "table":
        return frozenset(TABLE_PARAMS)
    return frozenset(STRUCTURE_PARAMS)


def _order_key(pipeline_id: str) -> tuple:
    """固定展示顺序；未登记的产线排在后面（按 id 字母序）。"""
    if pipeline_id in PIPELINE_ORDER:
        return (PIPELINE_ORDER.index(pipeline_id), pipeline_id)
    return (len(PIPELINE_ORDER), pipeline_id)


def build_registry(settings: Settings) -> List[PipelineSpec]:
    """扫描 config/pipelines/*.yaml + 内置 OCR，返回可用产线列表。"""
    specs: List[PipelineSpec] = []
    pipelines_dir = settings.config_path.parent / "pipelines"
    id_to_arg: Dict[str, str] = {}

    if pipelines_dir.is_dir():
        for yaml_path in sorted(pipelines_dir.glob("*.yaml")):
            text = read_config_text(yaml_path)
            if text is None:
                continue  # 已在 read_config_text 里记过日志（元数据/二进制/不可读）
            stem = yaml_path.stem
            meta = _read_meta(text)
            pipeline_name = _read_pipeline_name(text)
            kind = _resolve_kind(stem, pipeline_name, meta)
            specs.append(
                PipelineSpec(
                    id=stem,
                    label=meta.get("label") or DEFAULT_LABELS.get(stem, stem),
                    arg=str(yaml_path),
                    kind=kind,
                    description=meta.get("description") or DEFAULT_DESCRIPTIONS.get(stem, ""),
                    config_file=str(yaml_path.relative_to(PROJECT_ROOT))
                    if str(yaml_path).startswith(str(PROJECT_ROOT))
                    else str(yaml_path),
                    params=_params_for_kind(kind),
                )
            )
            id_to_arg[stem] = str(yaml_path)

    # 内置 OCR 兜底：若项目里已有 config/pipelines/OCR.yaml（PP-OCRv6），
    # 以 yaml 为准（可配档位/批大小），不要再追加同名产线造成重复。
    if not any(sp.id == "OCR" for sp in specs):
        specs.append(
            PipelineSpec(
                id="OCR",
                label=DEFAULT_LABELS["OCR"],
                arg="OCR",
                kind="ocr",
                description=DEFAULT_DESCRIPTIONS["OCR"],
                config_file="",
                params=frozenset(OCR_PARAMS),
            )
        )

    # 允许用 ocr.available_pipelines 限定展示/可用范围（例如只暴露精简版）
    allow = settings.get("ocr.available_pipelines") or []
    if isinstance(allow, list) and allow:
        allowed = {str(a) for a in allow}
        filtered = [s for s in specs if s.id in allowed]
        if filtered:
            specs = filtered

    # 兼容旧配置：ocr.pipeline_config 指定某个 yaml 时，覆盖默认产线的配置来源
    explicit = str(settings.get("ocr.pipeline_config", "") or "").strip()
    if explicit:
        cfg = Path(explicit).expanduser()
        if not cfg.is_absolute():
            cfg = (settings.config_path.parent.parent / cfg).resolve()
        if cfg.exists():
            default_id_tmp = resolve_default_id(settings, specs)
            rebuilt = []
            for spec in specs:
                if spec.id == default_id_tmp:
                    rebuilt.append(
                        PipelineSpec(
                            id=spec.id,
                            label=spec.label,
                            arg=str(cfg),
                            kind=spec.kind,
                            description=spec.description,
                            config_file=str(cfg),
                            params=_params_for_kind(spec.kind),
                        )
                    )
                else:
                    rebuilt.append(spec)
            specs = rebuilt

    # 固定展示顺序：纯文字 → 通用版面解析（精简）→ （完整）→ 通用表格识别
    # 注意：默认产线不再提到最前，否则会打乱用户要求的顺序；
    #      前端会按 ocr.pipeline 自动选中默认项，并在文案里标注「默认」。
    specs.sort(key=lambda sp: _order_key(sp.id))
    return specs


def resolve_default_id(settings: Settings, specs: List[PipelineSpec]) -> str:
    raw = str(settings.get("ocr.pipeline", "") or "").strip()
    if not raw:
        return specs[0].id if specs else "OCR"
    ids = [s.id for s in specs]
    if raw in ids:
        return raw
    # 兼容旧配置：ocr.pipeline 写的是 PaddleX 产线名（PP-StructureV3 / OCR）
    for spec in specs:
        if spec.id.lower() == raw.lower():
            return spec.id
    for spec in specs:
        if spec.id in ("PP-StructureV3",) or spec.is_structure:
            return spec.id
    return ids[0]


def find_spec(specs: List[PipelineSpec], pipeline_id: Optional[str]) -> Optional[PipelineSpec]:
    if not pipeline_id:
        return None
    for spec in specs:
        if spec.id == pipeline_id:
            return spec
    return None
