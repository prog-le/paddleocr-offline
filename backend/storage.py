"""任务目录存储：全部落本地目录，不依赖数据库。

目录结构：
    data/tasks/<task_id>/
        meta.json          任务元信息（状态、进度、统计）
        input/<原文件名>    上传的原件
        pages/p0001.jpg    每页渲染图（可选保留）
        raw/page-0001.json 每页 PaddleX 原始预测结果（排查用）
        result.json        规范化后的文档结构（导出与预览共用）
        exports/*.docx ... 导出的成品
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .settings import Settings

SAFE_NAME_RE = re.compile(r"[^\w\u4e00-\u9fff.\-]+")


def new_task_id() -> str:
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"


def safe_filename(name: str, fallback: str = "file") -> str:
    """去掉路径分隔符与危险字符，保留中文/字母/数字/点/下划线/连字符。"""
    name = os.path.basename(name or "").strip() or fallback
    name = SAFE_NAME_RE.sub("_", name)
    name = name.strip("._") or fallback
    return name[:120]


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp, path)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


class Storage:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.root = settings.tasks_dir
        self.exports = settings.exports_dir
        self.root.mkdir(parents=True, exist_ok=True)
        self.exports.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ paths
    def task_dir(self, task_id: str) -> Path:
        return self.root / task_id

    def meta_path(self, task_id: str) -> Path:
        return self.task_dir(task_id) / "meta.json"

    def result_path(self, task_id: str) -> Path:
        return self.task_dir(task_id) / "result.json"

    def input_dir(self, task_id: str) -> Path:
        return self.task_dir(task_id) / "input"

    def pages_dir(self, task_id: str) -> Path:
        return self.task_dir(task_id) / "pages"

    def raw_dir(self, task_id: str) -> Path:
        return self.task_dir(task_id) / "raw"

    def export_dir(self, task_id: str) -> Path:
        return self.task_dir(task_id) / "exports"

    # ------------------------------------------------------------------- meta
    def init_task(self, task_id: str, meta: Dict[str, Any]) -> Path:
        d = self.task_dir(task_id)
        for sub in ("input", "pages", "raw", "exports"):
            (d / sub).mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.meta_path(task_id), meta)
        return d

    def save_meta(self, meta: Dict[str, Any]) -> None:
        atomic_write_json(self.meta_path(meta["id"]), meta)

    def load_meta(self, task_id: str) -> Optional[Dict[str, Any]]:
        return read_json(self.meta_path(task_id))

    def iter_metas(self) -> Iterable[Dict[str, Any]]:
        if not self.root.exists():
            return []
        out: List[Dict[str, Any]] = []
        for child in self.root.iterdir():
            if not child.is_dir():
                continue
            meta = read_json(child / "meta.json")
            if isinstance(meta, dict) and meta.get("id"):
                out.append(meta)
        return out

    # ----------------------------------------------------------------- result
    def save_result(self, task_id: str, doc: Dict[str, Any]) -> None:
        atomic_write_json(self.result_path(task_id), doc)

    def load_result(self, task_id: str) -> Optional[Dict[str, Any]]:
        return read_json(self.result_path(task_id))

    # ----------------------------------------------------------------- delete
    def delete_task(self, task_id: str) -> bool:
        d = self.task_dir(task_id)
        if not d.exists():
            return False
        # 防止 ../ 之类越界
        if d.resolve().parent != self.root.resolve():
            return False
        shutil.rmtree(d, ignore_errors=True)
        # 导出缓存单独存放在 data/exports/<task_id>，一并清理
        export_dir = self.exports / task_id
        if export_dir.resolve().parent == self.exports.resolve():
            shutil.rmtree(export_dir, ignore_errors=True)
        return True

    # ---------------------------------------------------------------- cleanup
    def cleanup_expired(self, retention_hours: float, keep_ids: Optional[set] = None) -> List[str]:
        """删除超过保留期的任务目录，返回被删除的 task_id 列表。"""
        keep_ids = keep_ids or set()
        cutoff = time.time() - max(retention_hours, 0.1) * 3600
        removed: List[str] = []
        if not self.root.exists():
            return removed
        for child in self.root.iterdir():
            if not child.is_dir() or child.name in keep_ids:
                continue
            meta = read_json(child / "meta.json", {}) or {}
            ts = meta.get("created_at") or child.stat().st_mtime
            # 正在跑的任务绝不清理
            if meta.get("status") in ("running", "pending"):
                continue
            if ts < cutoff:
                shutil.rmtree(child, ignore_errors=True)
                export_dir = self.exports / child.name
                if export_dir.resolve().parent == self.exports.resolve():
                    shutil.rmtree(export_dir, ignore_errors=True)
                removed.append(child.name)
        return removed

    def disk_usage(self) -> Dict[str, int]:
        def _du(p: Path) -> int:
            total = 0
            for dirpath, _dirnames, filenames in os.walk(p):
                for fn in filenames:
                    try:
                        total += os.path.getsize(os.path.join(dirpath, fn))
                    except OSError:
                        pass
            return total

        cached = getattr(self, "_du_cache", None)
        if cached and time.time() - cached[0] < 15:
            return cached[1]

        usage = {
            "tasks_bytes": _du(self.root),
            "exports_bytes": _du(self.exports),
            "data_dir_bytes": _du(self.settings.data_dir),
        }
        self._du_cache = (time.time(), usage)
        return usage
