"""任务队列与执行器。

设计：
  * 进程内 ThreadPoolExecutor，队列长度为 concurrency，其余任务排队（FIFO）；
  * 每个任务独立目录，状态即时落盘（meta.json），进程重启后可见历史任务；
  * PDF 逐页渲染 + 逐页推理，每页结束回报进度并检查取消/超时；
  * 单任务日志写入 data/tasks/<id>/task.log，管理后台可直接查看。
"""

from __future__ import annotations

import os
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import devices as device_mod
from . import memguard
from .docmodel import Doc, Page
from .engine import EngineError
from .logging_setup import get_logger, task_logger
from .settings import Settings
from .storage import Storage, new_task_id, safe_filename

MAX_QUEUE_EXCEEDED = "队列已满，请稍后重试"


def _page_mem_text(on_gpu: bool, gpu_index: int = 0) -> str:
    """「第 N 页完成」日志里的内存/显存片段。

    单独抽成函数是为了避免"只在某个分支里赋值、日志却无条件使用"这类
    UnboundLocalError（线上真实故障：GPU 路径下 page_mem 未赋值，任务在
    第 1 页就失败，且只在有显卡的机器上出现，CPU/MOCK 测试完全测不到）。
    两个分支都必须返回字符串，异常也要兜住——日志不能把任务搞挂。
    """
    if on_gpu:
        try:
            info = device_mod.gpu_memory(gpu_index)
            return (
                f"显存池 {info.get('reserved_mb', '?')}MB"
                f"（峰值 {info.get('peak_reserved_mb', '?')}MB）"
                f"，整卡 {info.get('used_mem_mb', '?')}/{info.get('total_mem_mb', '?')}MB"
            )
        except Exception as exc:  # noqa: BLE001
            return f"显存信息不可用（{exc}）"
    try:
        memguard.note_rss()
        return memguard.format_snapshot()
    except Exception as exc:  # noqa: BLE001
        return f"内存信息不可用（{exc}）"


class TaskError(RuntimeError):
    """任务级可读错误（直接展示给用户）。"""


class _TaskLog:
    """单任务日志文件写入器。"""

    def __init__(self, path: Path, task_id: str):
        self.path = path
        self.task_id = task_id
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, message: str, level: str = "INFO") -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {level:<5} {message}\n"
        with self._lock:
            try:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line)
            except OSError:
                pass

    def tail(self, lines: int = 200) -> str:
        try:
            with open(self.path, "r", encoding="utf-8", errors="replace") as f:
                content = f.readlines()
            return "".join(content[-lines:])
        except OSError:
            return ""


class TaskManager:
    def __init__(self, settings: Settings, storage: Storage, engine: Any):
        self.settings = settings
        self.storage = storage
        # engine 可能是单个引擎，也可能是 EnginePool（多产线）
        self.engine = engine
        self.log = get_logger("ocr.manager")
        self._lock = threading.RLock()
        self._tasks: Dict[str, Dict[str, Any]] = {}
        self._futures: Dict[str, Any] = {}
        self._paused = False
        self._stop = threading.Event()
        self._started_at = time.time()
        self._total_done = 0
        self._total_failed = 0
        concurrency = max(int(settings.get("ocr.concurrency", 1) or 1), 1)
        self._executor = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="ocr")
        self.pipeline_name = getattr(engine, "pipeline_name", "")
        self.default_pipeline = getattr(engine, "default_id", self.pipeline_name)

        self._load_existing()
        self._cleanup_thread = threading.Thread(target=self._cleanup_loop, name="cleanup", daemon=True)
        self._cleanup_thread.start()

        if settings.get("ocr.warmup_on_start") and not getattr(engine, "is_mock", False):
            threading.Thread(target=self._warmup, name="warmup", daemon=True).start()

    # ---------------------------------------------------------------- 初始化
    def _load_existing(self) -> None:
        for meta in self.storage.iter_metas():
            tid = meta["id"]
            if meta.get("status") in ("running", "pending"):
                meta["status"] = "failed"
                meta["error"] = "服务重启，任务被中断，请重新上传或点击重试"
                meta["finished_at"] = time.time()
                meta["stage"] = "已中断"
                self.storage.save_meta(meta)
            self._tasks[tid] = meta
        if self._tasks:
            self.log.info("已加载历史任务 %d 条", len(self._tasks))

    def _warmup(self) -> None:
        try:
            self.engine.ensure_loaded()
        except Exception as exc:  # noqa: BLE001
            self.log.error("预热失败：%s", exc)

    def _cleanup_loop(self) -> None:
        interval = int(self.settings.get("storage.cleanup_interval_sec", 900) or 900)
        while not self._stop.wait(max(interval, 30)):
            try:
                self.cleanup_expired()
            except Exception as exc:  # noqa: BLE001
                self.log.warning("清理失败：%s", exc)

    # ------------------------------------------------------------------ 查询
    def get(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            meta = self._tasks.get(task_id)
            return dict(meta) if meta else None

    def get_owned(self, task_id: str, owner: Optional[str]) -> Optional[Dict[str, Any]]:
        """按会话归属取任务；不属于该会话（或不存在）一律返回 None。

        对外统一表现为"任务不存在"，避免通过 403/404 差异探测他人任务是否存在。
        """
        meta = self.get(task_id)
        if not meta:
            return None
        if not owner or meta.get("owner") != owner:
            return None
        return meta

    def result(self, task_id: str) -> Optional[Dict[str, Any]]:
        return self.storage.load_result(task_id)

    def task_log_tail(self, task_id: str, lines: int = 200) -> str:
        return _TaskLog(self.storage.task_dir(task_id) / "task.log", task_id).tail(lines)

    def list_tasks(
        self,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        keyword: Optional[str] = None,
        owner: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            items = [dict(m) for m in self._tasks.values()]
        if owner is not None:
            items = [m for m in items if m.get("owner") == owner]
        if status and status != "all":
            if status == "active":
                items = [m for m in items if m.get("status") in ("pending", "running")]
            else:
                items = [m for m in items if m.get("status") == status]
        if keyword:
            kw = keyword.lower()
            items = [
                m
                for m in items
                if kw in str(m.get("filename", "")).lower() or kw in str(m.get("id", "")).lower()
            ]
        items.sort(key=lambda m: m.get("created_at", 0), reverse=True)
        total = len(items)
        page = items[offset : offset + limit]
        for item in page:
            item.update(self._queue_info(item))
        return {"total": total, "items": page}

    def _queue_info(self, meta: Dict[str, Any]) -> Dict[str, Any]:
        if meta.get("status") != "pending":
            return {}
        with self._lock:
            pending = sorted(
                (m for m in self._tasks.values() if m.get("status") == "pending"),
                key=lambda m: m.get("created_at", 0),
            )
            try:
                position = [m["id"] for m in pending].index(meta["id"]) + 1
            except ValueError:
                position = None
            running = sum(1 for m in self._tasks.values() if m.get("status") == "running")
        return {"queue_position": position, "running_count": running}

    def stats(self, owner: Optional[str] = None) -> Dict[str, Any]:
        with self._lock:
            items = list(self._tasks.values())
        if owner is not None:
            items = [m for m in items if m.get("owner") == owner]
        queue_depth = sum(1 for m in items if m.get("status") == "pending")
        running = sum(1 for m in items if m.get("status") == "running")
        counts: Dict[str, int] = {}
        durations: List[float] = []
        pages_total = 0
        today = time.strftime("%Y-%m-%d")
        today_count = 0
        for m in items:
            status = str(m.get("status"))
            counts[status] = counts.get(status, 0) + 1
            if m.get("duration_sec"):
                durations.append(float(m["duration_sec"]))
            pages_total += int(m.get("pages_total") or 0)
            if time.strftime("%Y-%m-%d", time.localtime(m.get("created_at") or 0)) == today:
                today_count += 1
        return {
            "total": len(items),
            "pending": counts.get("pending", 0),
            "running": running,
            "success": counts.get("success", 0),
            "failed": counts.get("failed", 0),
            "cancelled": counts.get("cancelled", 0),
            "today": today_count,
            "pages_total": pages_total,
            "avg_duration_sec": round(sum(durations) / len(durations), 2) if durations else 0,
            "queue_depth": queue_depth,
            "concurrency": int(self.settings.get("ocr.concurrency", 1) or 1),
            "paused": self._paused,
            "uptime_sec": int(time.time() - self._started_at),
        }

    # ------------------------------------------------------------------ 提交
    def submit(
        self,
        input_path: Path,
        filename: str,
        size: int,
        params: Dict[str, Any],
        batch_id: Optional[str] = None,
        source_type: str = "",
        owner: Optional[str] = None,
        pipeline: Optional[str] = None,
        pipeline_label: Optional[str] = None,
    ) -> Dict[str, Any]:
        if self._paused:
            raise TaskError("服务已暂停接收新任务，请联系管理员")
        with self._lock:
            pending = sum(1 for m in self._tasks.values() if m.get("status") == "pending")
            max_queue = int(self.settings.get("limits.queue_max_size", 100) or 100)
            if pending >= max_queue:
                raise TaskError(MAX_QUEUE_EXCEEDED)
            task_id = new_task_id()
            meta = {
                "id": task_id,
                "batch_id": batch_id or task_id,
                "filename": filename,
                "stored_name": input_path.name if isinstance(input_path, Path) else str(input_path),
                "size": int(size),
                "source_type": source_type or ("pdf" if filename.lower().endswith(".pdf") else "image"),
                "status": "pending",
                "progress": 0,
                "stage": "排队中",
                "pages_total": 0,
                "pages_done": 0,
                "created_at": time.time(),
                "started_at": None,
                "finished_at": None,
                "duration_sec": None,
                "error": None,
                "params": params or {},
                "pipeline": pipeline or self.default_pipeline,
                "pipeline_label": pipeline_label or (pipeline or self.default_pipeline),
                "retry_of": None,
                # 归属会话：公开接口按此过滤，管理员接口忽略
                "owner": owner or "",
            }
            self.storage.init_task(task_id, meta)
            # 把暂存文件移入任务目录（staged_path 由上传接口写入 data/uploads/）
            try:
                staged = Path(input_path)
                if staged.exists():
                    dest = self.storage.input_dir(task_id) / safe_filename(filename, "upload")
                    os.replace(staged, dest)
                    meta["stored_name"] = dest.name
                    self.storage.save_meta(meta)
            except OSError as exc:
                raise TaskError(f"保存上传文件失败：{exc}") from exc
            self._tasks[task_id] = meta
        self._dispatch(task_id)
        return self.get(task_id) or meta

    def _dispatch(self, task_id: str) -> None:
        future = self._executor.submit(self._run, task_id)
        with self._lock:
            self._futures[task_id] = future

    # ------------------------------------------------------------------ 执行
    def _update(self, task_id: str, persist: bool = False, **fields: Any) -> Dict[str, Any]:
        with self._lock:
            meta = self._tasks.get(task_id)
            if meta is None:
                return {}
            meta.update(fields)
            snapshot = dict(meta)
        if persist:
            self.storage.save_meta(snapshot)
        return snapshot

    def _run(self, task_id: str) -> None:
        meta = self.get(task_id)
        if not meta:
            return
        tlog = task_logger(task_id)
        tfile = _TaskLog(self.storage.task_dir(task_id) / "task.log", task_id)
        started = time.time()
        max_pages = int(self.settings.get("limits.max_pdf_pages", 100) or 100)
        timeout = int(self.settings.get("limits.task_timeout_sec", 1800) or 1800)

        def stage(text: str, progress: Optional[float] = None) -> None:
            fields: Dict[str, Any] = {"stage": text}
            if progress is not None:
                fields["progress"] = round(min(max(progress, 0), 100), 1)
            self._update(task_id, persist=True, **fields)
            tfile.write(text)
            tlog.info(text)

        try:
            if meta.get("cancel_requested"):
                raise _Cancelled()
            self._update(task_id, status="running", started_at=started)
            pipeline_id = meta.get("pipeline") or self.default_pipeline
            engine = self.engine.get(pipeline_id) if hasattr(self.engine, "get") else self.engine
            stage(f"加载识别模型（{getattr(engine, 'label', pipeline_id)}）…", 1)
            engine.ensure_loaded()

            stage("解析输入文件…", 3)
            from . import pdfio

            input_dir = self.storage.input_dir(task_id)
            files = sorted(input_dir.glob("*"))
            if not files:
                raise TaskError("上传文件丢失，请重新上传")
            src = files[0]
            kind, pages_total = pdfio.probe(src)
            if pages_total <= 0:
                raise TaskError("文件页数为 0，可能已损坏")
            if kind == "pdf" and pages_total > max_pages:
                raise TaskError(
                    f"PDF 共 {pages_total} 页，超过单任务上限 {max_pages} 页。"
                    "请拆分文件后重试，或在 config/config.yaml 调整 limits.max_pdf_pages"
                )
            self._update(task_id, pages_total=pages_total, source_type=kind, persist=True)
            tfile.write(f"文件类型={kind} 页数={pages_total} 大小={meta.get('size')}B")
            tlog.info("开始处理 %s（%s，%s 页）", meta.get("filename"), kind, pages_total)

            # pdf-inspector（Rust 原生抽取）：吃完整 PDF 一次性解析，不适合逐页渲染。
            # 图片（kind != pdf）不路由到这里。路由完成后 doc.pages 会被填充，
            # 后续逐页 OCR 循环会因为 pages 已被填满而被跳过。
            backend = getattr(engine, "backend", "paddlex")
            pdf_inspector_done = False
            if backend == "pdf_inspector" and kind == "pdf":
                self._run_pdf_inspector(
                    task_id, src, pages_total, params, engine, doc, tfile, tlog
                )
                pdf_inspector_done = True

            dpi = int(self.settings.get("ocr.pdf_dpi", 200) or 200)
            max_side = int(self.settings.get("ocr.max_side_px", 2400) or 0)
            max_pixels = int(self.settings.get("limits.max_image_pixels", 50_000_000) or 0)
            min_free_mb = int(self.settings.get("limits.min_free_mem_mb", 600) or 0)
            params = meta.get("params") or {}

            # 设备与内存基线：GPU 看显存池，CPU 看 cgroup 余量
            device_info = getattr(engine, "device_info", {}) or {}
            on_gpu = device_info.get("kind") == "gpu"
            gpu_index = int(device_info.get("index") or 0)
            if on_gpu:
                # 显存基线只是诊断信息：nvidia-smi 缺失/受限（容器、MIG、PATH 被裁剪）
                # 都不能让任务失败——真正跑不跑得动由推理本身决定。
                tfile.write(f"运行设备：{device_info.get('device')} · {device_info.get('name')}")
                try:
                    gpu_mem = device_mod.gpu_memory(gpu_index)
                    device_mod.reset_peak_memory(gpu_index)
                    tfile.write(
                        "显存基线："
                        f"整卡 {gpu_mem.get('used_mem_mb', '?')}/{gpu_mem.get('total_mem_mb', '?')}MB 已用，"
                        f"本进程池 reserved={gpu_mem.get('reserved_mb', '?')}MB"
                    )
                    tlog.info("任务开始 %s | GPU %s | %s", task_id, device_info.get("name"), gpu_mem)
                except Exception as exc:  # noqa: BLE001
                    tfile.write(f"显存基线读取失败（不影响识别）：{exc}", "WARN")
                    tlog.warning("任务开始 %s | GPU %s | 显存信息读取失败：%s",
                                 task_id, device_info.get("name"), exc)
            else:
                meminfo = memguard.snapshot()
                mem_line = memguard.format_snapshot(meminfo)
                tfile.write(f"内存基线：{mem_line}")
                tlog.info("任务开始 %s | %s", task_id, mem_line)
                if meminfo.get("limit_mb") and meminfo["limit_mb"] < 4096:
                    tfile.write(
                        f"容器内存上限仅 {meminfo['limit_mb']:.0f}MB，识别大页面可能被 OOM kill；"
                        "建议 docker/.env 里 MEM_LIMIT 调到 8g 以上",
                        "WARN",
                    )

            doc = Doc(
                name=meta.get("filename", ""),
                source_type=kind,
                pipeline=f"{getattr(engine, 'label', pipeline_id)}（{pipeline_id}）",
                params=params,
            )

            for index in range(pages_total):
                # pdf-inspector 已完成整 PDF 快抽，跳过逐页渲染 + OCR 循环
                if pdf_inspector_done:
                    break
                current = self.get(task_id) or {}
                if current.get("cancel_requested"):
                    raise _Cancelled()
                if time.time() - started > timeout:
                    raise _Timeout(
                        f"任务超时（>{timeout}s），已处理 {index}/{pages_total} 页。"
                        "可降低 ocr.pdf_dpi 或拆分文件，或调大 limits.task_timeout_sec"
                    )
                page_no = index + 1
                base_progress = 5 + (index / pages_total) * 90
                stage(f"第 {page_no}/{pages_total} 页：渲染图像…", base_progress)
                page_path = self.storage.pages_dir(task_id) / f"p{page_no:04d}.jpg"
                try:
                    width, height = pdfio.render_page(
                        src, kind, index, page_path, dpi=dpi, max_side=max_side, max_pixels=max_pixels
                    )
                except Exception as exc:  # noqa: BLE001
                    if pages_total == 1:
                        raise TaskError(str(exc)) from exc
                    # 单页渲染失败不拖垮整份文档
                    doc.warnings.append(f"第 {page_no} 页渲染失败：{exc}")
                    tfile.write(f"第 {page_no} 页渲染失败：{exc}", "ERROR")
                    self._update(task_id, pages_done=page_no, persist=True)
                    continue

                # 推理前检查余量：宁可让这一页失败，也不要被内核 OOM kill 掉整个容器/进程。
                # GPU 的显存由 CUDA 分配器管理，cgroup 检查不适用；显存溢出由引擎捕获后
                # 给出“调小 batch / 像素 / 限流”的可操作错误。
                if not on_gpu:
                    need_mb = memguard.estimate_page_mb(width, height)
                    try:
                        memguard.ensure_headroom(need_mb, min_free_mb)
                    except memguard.MemError as exc:
                        tfile.write(f"第 {page_no} 页内存不足：{need_mb}MB 需求，{exc}", "ERROR")
                        raise TaskError(f"第 {page_no}/{pages_total} 页因内存不足中止：{exc}") from exc

                stage(f"第 {page_no}/{pages_total} 页：OCR + 表格识别…", base_progress + 2)
                try:
                    page_result = engine.predict_page(page_path, params)
                except EngineError as exc:
                    raise TaskError(str(exc)) from exc

                if self.settings.get("storage.keep_raw_json", True) and page_result.raw:
                    from .storage import atomic_write_json

                    atomic_write_json(
                        self.storage.raw_dir(task_id) / f"page-{page_no:04d}.json", page_result.raw
                    )

                page = Page(
                    index=page_no,
                    width=page_result.width or width,
                    height=page_result.height or height,
                    blocks=page_result.blocks,
                    elapsed_ms=page_result.elapsed_ms,
                )
                doc.pages.append(page)
                done_progress = 5 + ((index + 1) / pages_total) * 90
                self._update(task_id, pages_done=page_no, persist=True)
                page_mem = _page_mem_text(on_gpu, gpu_index)
                tfile.write(f"第 {page_no} 页完成 | {page_mem}")
                tlog.info("第 %d/%d 页 %s（%d ms）| %s", page_no, pages_total,
                          f"{len(page.blocks)} 块/{len(page.tables)} 表", page_result.elapsed_ms, page_mem)
                stage(
                    f"第 {page_no}/{pages_total} 页完成（{len(page.blocks)} 个块，{len(page.tables)} 个表格，"
                    f"{page_result.elapsed_ms} ms）",
                    done_progress,
                )

            if not doc.pages:
                raise TaskError("所有页面处理失败，请检查文件是否有效")

            stage("整理识别结果…", 96)
            stats = doc.stats()
            self.storage.save_result(task_id, doc.to_dict())

            if not self.settings.get("storage.keep_page_images", True):
                for img in self.storage.pages_dir(task_id).glob("*.jpg"):
                    try:
                        img.unlink()
                    except OSError:
                        pass

            duration = round(time.time() - started, 2)
            self._total_done += 1
            self._update(
                task_id,
                status="success",
                progress=100,
                stage="识别完成",
                finished_at=time.time(),
                duration_sec=duration,
                stats=stats,
                persist=True,
            )
            mem_peak = memguard.snapshot()
            tfile.write(f"完成：{stats}，耗时 {duration}s | 内存峰值 {mem_peak.get('peak_mb') or 0:.0f}MB")
            if on_gpu:
                # GPU 场景补一行显存收尾（CPU 的 RSS/可用内存对排查显存问题没用）
                tfile.write(f"显存收尾 | {_page_mem_text(True, gpu_index)}")
            tlog.info("任务完成 %s %s 耗时 %.2fs | %s", task_id, stats, duration,
                      memguard.format_snapshot(mem_peak))
            memguard.reset_peak()

        except _Cancelled:
            self._update(
                task_id,
                status="cancelled",
                stage="已取消",
                finished_at=time.time(),
                duration_sec=round(time.time() - started, 2),
                error="任务已被取消",
                persist=True,
            )
            tfile.write("任务被取消", "WARN")
            tlog.warning("任务被取消 %s", task_id)
        except (_Timeout, TaskError) as exc:
            self._fail(task_id, str(exc), tfile, tlog)
        except Exception as exc:  # noqa: BLE001
            detail = traceback.format_exc()
            tfile.write(detail, "ERROR")
            tlog.error("任务异常 %s\n%s", task_id, detail)
            self._fail(task_id, f"识别失败：{exc}", tfile, tlog, detail)
        finally:
            with self._lock:
                self._futures.pop(task_id, None)
            memguard.reset_peak()

    def _fail(self, task_id: str, message: str, tfile: _TaskLog, tlog, detail: str = "") -> None:
        self._total_failed += 1
        self._update(
            task_id,
            status="failed",
            stage="失败",
            error=message,
            error_detail=detail[-4000:] if detail else "",
            finished_at=time.time(),
            persist=True,
        )
        tfile.write(message, "ERROR")
        tlog.error("任务失败 %s: %s", task_id, message)

    # ------------------------------------------------------------------ pdf-inspector 快抽路径
    def _run_pdf_inspector(
        self,
        task_id: str,
        src: Path,
        pages_total: int,
        params: Dict[str, Any],
        engine: Any,
        doc: Any,
        tfile: Any,
        tlog: Any,
    ) -> Dict[str, Any]:
        """pdf-inspector 专用整 PDF 路径。

        pdf-inspector 的 Rust 实现一次性解析整 PDF 拿到分类 + Markdown，
        跨页字体统计影响 header 检测，所以必须整文件调用 ``predict_pdf``，
        而不是 ``run`` 里逐页渲染 + ``predict_page``。
        """
        from .docmodel import Page

        tfile.write("使用 PDF Inspector（Rust 快抽）一次性解析整 PDF", "INFO")
        self._update(task_id, progress=10, pages_done=0, stage="PDF Inspector 分类与抽取…", persist=True)

        started = time.time()
        try:
            result = engine.predict_pdf(src, params)
        except Exception as exc:  # noqa: BLE001 - EngineError 已经包装好；其他异常兜底
            detail = traceback.format_exc()
            self._fail(
                task_id,
                message=f"pdf-inspector 解析失败：{exc}",
                detail=detail,
                tfile=tfile,
                tlog=tlog,
            )
            raise TaskError(str(exc)) from exc

        blocks = result.blocks or []
        elapsed_ms = int((time.time() - started) * 1000)
        raw = result.raw or {}
        pdf_type = raw.get("pdf_type", "unknown")
        confidence = raw.get("confidence", 0.0)
        pages_needing_ocr = raw.get("pages_needing_ocr", []) or []

        tfile.write(
            f"pdf-inspector 完成 | 类型={pdf_type} 置信度={confidence:.2f} "
            f"需 OCR 页数={len(pages_needing_ocr)} 耗时={elapsed_ms}ms"
        )
        if pages_needing_ocr:
            tfile.write(
                f"以下 {len(pages_needing_ocr)} 页被判定为扫描/图片页："
                f"{[p + 1 for p in pages_needing_ocr]}（按 1-index 显示）。"
                "当前 pdf-inspector 产线只做快抽，不对扫描页做 OCR；"
                "换用 PP-StructureV3 等 PaddleX 产线可自动处理。",
                "WARN",
            )

        if blocks:
            if self.settings.get("storage.keep_raw_json", True) and raw:
                from .storage import atomic_write_json

                atomic_write_json(
                    self.storage.raw_dir(task_id) / "document.json",
                    raw,
                )
            # pdf-inspector 不区分页——输出是整 PDF 的 Markdown。
            # 为了保持与 PaddleX 逐页模式一致，我们把同一份 blocks 挂到每个 Page 上，
            # 并把 raw / 总耗时挂到最后一页的 raw 里供前端展示。
            for i in range(pages_total):
                page_no = i + 1
                is_last = page_no == pages_total
                page_raw = dict(raw) if is_last else None
                page_blocks = list(blocks)  # 全量 blocks 复制给每页——导出时用户可见，单页预览会稍长
                page = Page(
                    index=page_no,
                    width=0,
                    height=0,
                    blocks=page_blocks,
                    elapsed_ms=elapsed_ms if is_last else 0,
                    raw=page_raw,
                )
                doc.pages.append(page)

            # 快抽产线没有逐页进度——直接把 progress 提到 95%（剩下 5% 是导出）
            self._update(
                task_id,
                progress=95,
                pages_done=pages_total,
                pages_total=pages_total,
                stage=f"分类={pdf_type}（{confidence:.0%}）blocks={len(blocks)}，正在导出…",
                persist=True,
            )
        else:
            # 抽取不到内容（全扫描 / 空 PDF）
            tfile.write(
                "未抽到任何文本块——可能全是扫描/图片页。"
                "换用 PP-StructureV3 产线可自动处理。",
                "WARN",
            )
            for i in range(pages_total):
                page_no = i + 1
                is_last = page_no == pages_total
                page = Page(
                    index=page_no,
                    width=0,
                    height=0,
                    blocks=[],
                    elapsed_ms=elapsed_ms if is_last else 0,
                    raw=dict(raw) if is_last else None,
                )
                doc.pages.append(page)

        self._update(task_id, doc=doc, pages_done=pages_total, progress=95, persist=True)
        # 填充 doc.pages 结束；主 run 后续会继续做 save_result / stats / 导出

    # ------------------------------------------------------------------ 控制
    def cancel(self, task_id: str) -> Dict[str, Any]:
        meta = self.get(task_id)
        if not meta:
            raise TaskError("任务不存在")
        if meta["status"] in ("success", "failed", "cancelled"):
            return meta
        if meta["status"] == "pending":
            self._update(task_id, status="cancelled", stage="已取消", cancel_requested=True,
                         finished_at=time.time(), error="任务已被取消", persist=True)
        else:
            self._update(task_id, cancel_requested=True, stage="正在取消（当前页结束后停止）", persist=True)
        return self.get(task_id) or meta

    def delete(self, task_id: str) -> bool:
        meta = self.get(task_id)
        if not meta:
            return False
        if meta["status"] == "running":
            raise TaskError("任务正在识别，请先取消，待状态变为已取消后再删除")
        with self._lock:
            self._tasks.pop(task_id, None)
        return self.storage.delete_task(task_id)

    def retry(self, task_id: str) -> Dict[str, Any]:
        meta = self.get(task_id)
        if not meta:
            raise TaskError("任务不存在")
        if meta["status"] in ("pending", "running"):
            raise TaskError("任务正在队列中，无需重试")
        with self._lock:
            pending = sum(1 for m in self._tasks.values() if m.get("status") == "pending")
            if pending >= int(self.settings.get("limits.queue_max_size", 100) or 100):
                raise TaskError(MAX_QUEUE_EXCEEDED)
        # 清理旧产物，保留输入文件
        import shutil

        for sub in ("pages", "raw", "exports"):
            shutil.rmtree(self.storage.task_dir(task_id) / sub, ignore_errors=True)
        for name in ("result.json", "task.log"):
            try:
                (self.storage.task_dir(task_id) / name).unlink()
            except OSError:
                pass
        self._update(
            task_id,
            status="pending",
            progress=0,
            stage="排队中",
            pages_total=0,
            pages_done=0,
            started_at=None,
            finished_at=None,
            duration_sec=None,
            error=None,
            error_detail=None,
            cancel_requested=False,
            stats=None,
            retry_of=None,
            persist=True,
        )
        self._dispatch(task_id)
        return self.get(task_id) or meta

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    @property
    def paused(self) -> bool:
        return self._paused

    def cleanup_expired(self) -> List[str]:
        keep = {m["id"] for m in self._tasks.values() if m.get("status") in ("pending", "running")}
        removed = self.storage.cleanup_expired(
            float(self.settings.get("storage.retention_hours", 24) or 24), keep
        )
        if removed:
            with self._lock:
                for tid in removed:
                    self._tasks.pop(tid, None)
            self.log.info("清理过期任务 %d 个", len(removed))
        return removed

    def shutdown(self) -> None:
        self._stop.set()
        with self._lock:
            for meta in self._tasks.values():
                if meta.get("status") in ("pending", "running"):
                    meta["cancel_requested"] = True
        self._executor.shutdown(wait=False, cancel_futures=True)
        try:
            self.engine.close()
        except Exception:  # noqa: BLE001
            pass


class _Cancelled(Exception):
    pass


class _Timeout(Exception):
    pass
