"""配置加载：YAML + 环境变量覆盖 + 运行时可调覆盖层。

设计要点：
1. 全部路径统一解析为绝对路径，避免 uvicorn/Docker 不同工作目录导致的路径漂移。
2. PaddleX 相关环境变量必须在 `import paddlex` 之前写入，因此这里提供
   `apply_paddlex_env()`，由 backend.main 在导入引擎前调用。
3. 管理后台可在线修改的少量参数写入 data/runtime_overrides.json，
   不污染 config/config.yaml（便于容器内只读挂载配置文件）。
"""

from __future__ import annotations

import copy
import json
import os
import platform
import re
import sys
import secrets
from pathlib import Path
from typing import Any, Dict, Optional

try:
    import yaml
except ImportError:  # pragma: no cover - 依赖缺失时给出明确提示
    raise SystemExit("缺少 PyYAML，请先执行离线依赖安装（scripts/install_offline.sh）")

from .textio import read_text_file

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULTS: Dict[str, Any] = {
    "version": "1.0.0",
    "server": {
        "host": "0.0.0.0",
        "port": 8000,
        "admin_username": "admin",
        "admin_password": "PaddleX@2026",
        "session_secret": "",
        "session_ttl_sec": 7200,
        "max_login_attempts": 10,
        "login_lock_sec": 300,
        # 前台匿名会话：不同浏览器/不同人之间的任务隔离（Cookie 名 + 有效期）
        "client_cookie_name": "pdx_sid",
        "client_session_ttl_hours": 24,
        "root_path": "",
    },
    "limits": {
        "max_upload_mb": 200,
        "max_files_per_batch": 20,
        "max_pdf_pages": 100,
        "max_image_pixels": 50_000_000,
        "task_timeout_sec": 7200,
        "queue_max_size": 100,
    },
    "ocr": {
        "pipeline": "PP-StructureV3",
        # 留空则自动查找 config/pipelines/<pipeline>.yaml（存在则用项目内置配置）
        "pipeline_config": "",
        # 运行设备：auto / cpu / gpu / gpu:0 / gpu:1 / xpu:0 / npu:0
        # auto = 有可用加速卡就用，否则 CPU；macOS(Apple Silicon) 会强制 CPU（Paddle 无 Metal 后端）
        "device": "auto",
        # 显存池上限：gpu_mem_fraction 直接生效（0~0.95，0=用 Paddle 默认）；
        # gpu_mem_limit_mb 是便捷写法，按卡总显存换算 fraction（fraction>0 时优先）
        "gpu_mem_fraction": 0.0,
        "gpu_mem_limit_mb": 0,
        # OCR 产线档位（PP-OCRv6）：tiny / small / medium
        "ocr_tier": "medium",
        # 批处理：文本识别/文本行方向的 batch size（GPU 上可调大，CPU 建议 1~8）
        "text_rec_batch_size": 6,
        "textline_batch_size": 6,
        "concurrency": 1,
        "cpu_threads": 0,
        "pdf_dpi": 200,
        "max_side_px": 2400,
        "warmup_on_start": True,
        "defaults": {
            "use_doc_orientation_classify": False,
            "use_doc_unwarping": False,
            "use_textline_orientation": False,
            "use_table_recognition": True,
            "use_formula_recognition": False,
            "use_seal_recognition": False,
            "use_chart_recognition": False,
            "layout_threshold": 0.5,
            "text_det_limit_side_len": 960,
            "text_det_limit_type": "min",
            "text_det_thresh": 0.3,
            "text_det_box_thresh": 0.6,
            "text_det_unclip_ratio": 2.0,
            "text_rec_score_thresh": 0.0,
        },
    },
    # 可选产线：Indra OCR（轻量 CPU 模型，需另行 pip install indra-ocr）
    "indra": {
        "enabled": True,
        # 留空 = 使用 indra-ocr 自带缓存；离线部署建议指向已下载的目录，如 models/indra
        "model_dir": "",
        # ONNX Runtime 线程数：0 = 使用 indra-ocr 默认值（4）
        "threads": 0,
        "math": False,
        "keep_furniture": False,
    },
    "storage": {
        "data_dir": "data",
        "log_dir": "logs",
        "retention_hours": 24,
        "cleanup_interval_sec": 900,
        "keep_page_images": True,
        "keep_raw_json": True,
    },
    "export": {
        "include_noise_blocks": False,
        "txt_page_markers": True,
        "docx_page_break": True,
    },
    "logging": {"level": "INFO", "max_bytes": 10 * 1024 * 1024, "backup_count": 5},
}

# 允许通过环境变量覆盖的键（容器/多实例部署常用）
ENV_MAP = {
    "PADDLEX_OCR_HOST": "server.host",
    "PADDLEX_OCR_PORT": "server.port",
    "PADDLEX_OCR_ROOT_PATH": "server.root_path",
    "PADDLEX_OCR_ADMIN_USER": "server.admin_username",
    "PADDLEX_OCR_ADMIN_PASSWORD": "server.admin_password",
    "PADDLEX_OCR_SESSION_SECRET": "server.session_secret",
    "PADDLEX_OCR_CLIENT_TTL_HOURS": "server.client_session_ttl_hours",
    "PADDLEX_OCR_DATA_DIR": "storage.data_dir",
    "PADDLEX_OCR_LOG_DIR": "storage.log_dir",
    "PADDLEX_OCR_DEVICE": "ocr.device",
    "PADDLEX_OCR_GPU_MEM_MB": "ocr.gpu_mem_limit_mb",
    "PADDLEX_OCR_GPU_MEM_FRACTION": "ocr.gpu_mem_fraction",
    "PADDLEX_OCR_TEXT_REC_BATCH": "ocr.text_rec_batch_size",
    "PADDLEX_OCR_TIER": "ocr.ocr_tier",
    "PADDLEX_OCR_CONCURRENCY": "ocr.concurrency",
    "PADDLEX_OCR_PIPELINE": "ocr.pipeline",
    "PADDLEX_OCR_PDF_DPI": "ocr.pdf_dpi",
    "PADDLEX_OCR_MAX_UPLOAD_MB": "limits.max_upload_mb",
    "PADDLEX_OCR_RETENTION_HOURS": "storage.retention_hours",
    "PADDLEX_OCR_LOG_LEVEL": "logging.level",
    # Indra OCR（可选产线）
    "PADDLEX_OCR_INDRA_ENABLED": "indra.enabled",
    "PADDLEX_OCR_INDRA_MODEL_DIR": "indra.model_dir",
    "PADDLEX_OCR_INDRA_THREADS": "indra.threads",
    # 模型缓存目录（离线核心）：默认 <项目>/models/paddlex_home
    "PADDLEX_OCR_MODEL_HOME": "_model_home",
}

# 管理后台允许在线修改的键（其余需改配置文件并重启）
_DEVICE_SPEC_RE = re.compile(r"^(auto|cpu|gpu|cuda|xpu|npu|mlu|dcu|mps)(:\d+)?$")


RUNTIME_EDITABLE = (
    "ocr.device",
    "ocr.gpu_mem_limit_mb",
    "ocr.gpu_mem_fraction",
    "ocr.ocr_tier",
    "ocr.text_rec_batch_size",
    "ocr.textline_batch_size",
    "ocr.cpu_threads",
    "ocr.defaults",
    "limits.max_upload_mb",
    "limits.max_files_per_batch",
    "limits.max_pdf_pages",
    "limits.task_timeout_sec",
    "limits.queue_max_size",
    "storage.retention_hours",
    "export.include_noise_blocks",
    "export.txt_page_markers",
    "export.docx_page_break",
)


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _abs_path(value: str) -> Path:
    p = Path(str(value)).expanduser()
    return p if p.is_absolute() else (PROJECT_ROOT / p).resolve()


class Settings:
    """全局配置对象（进程内单例，见 get_settings）。"""

    def __init__(self, config_path: Optional[str] = None):
        self.config_path = Path(
            config_path or os.environ.get("PADDLEX_OCR_CONFIG") or PROJECT_ROOT / "config" / "config.yaml"
        ).expanduser()
        self.raw: Dict[str, Any] = copy.deepcopy(DEFAULTS)

        if self.config_path.exists():
            # 容错读取：目标机上这个文件可能被编辑器存成 GBK，或混入 macOS 元数据。
            # 直接 open(encoding="utf-8") 会让整个进程在启动阶段崩掉（且报错信息很难懂）。
            text = read_text_file(self.config_path, what="主配置文件")
            if text is None:
                raise SystemExit(
                    f"配置文件不是可读的 UTF-8/GBK 文本：{self.config_path}\n"
                    f"请用编辑器另存为 UTF-8，或从离线包重新拷贝 config/config.yaml"
                )
            try:
                loaded = yaml.safe_load(text) or {}
            except yaml.YAMLError as exc:
                raise SystemExit(f"配置文件 YAML 语法错误：{self.config_path}\n  {exc}") from exc
            if not isinstance(loaded, dict):
                raise SystemExit(f"配置文件格式错误（顶层必须是映射）：{self.config_path}")
            self.raw = _deep_merge(self.raw, loaded)

        # 先应用环境变量覆盖（PADDLEX_OCR_DATA_DIR 等），再解析路径，
        # 否则容器/测试用环境变量指定目录会被忽略
        self._apply_env()

        # 路径类配置统一绝对化
        self.data_dir = _abs_path(self.raw["storage"]["data_dir"])
        self.log_dir = _abs_path(self.raw["storage"]["log_dir"])
        model_home = os.environ.get("PADDLEX_OCR_MODEL_HOME")
        self.model_home = _abs_path(model_home) if model_home else (PROJECT_ROOT / "models" / "paddlex_home")
        self.frontend_dir = PROJECT_ROOT / "frontend"

        self._load_runtime_overrides()

        for d in (self.data_dir, self.log_dir, self.model_home):
            d.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "tasks").mkdir(parents=True, exist_ok=True)
        self._ensure_secret()

    # ------------------------------------------------------------------ utils
    def _set(self, dotted: str, value: Any) -> None:
        node = self.raw
        parts = dotted.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def _apply_env(self) -> None:
        for env_key, dotted in ENV_MAP.items():
            if env_key not in os.environ or dotted == "_model_home":
                continue
            raw_value = os.environ[env_key]
            if raw_value == "":
                continue
            current = self.get(dotted)
            try:
                if isinstance(current, bool):
                    value: Any = raw_value.strip().lower() in ("1", "true", "yes", "on")
                elif isinstance(current, int):
                    value = int(raw_value)
                elif isinstance(current, float):
                    value = float(raw_value)
                else:
                    value = raw_value
            except ValueError:
                value = raw_value
            self._set(dotted, value)

    # -------------------------------------------------------- runtime overlay
    @property
    def _overrides_path(self) -> Path:
        return self.data_dir / "runtime_overrides.json"

    def _load_runtime_overrides(self) -> None:
        if not self._overrides_path.exists():
            return
        try:
            with open(self._overrides_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return
        if isinstance(data, dict):
            self.raw = _deep_merge(self.raw, data)

    def update_runtime(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        """应用运行时覆盖（仅白名单键），返回实际生效的覆盖字典。

        兼容两种入参：
          * 嵌套结构（后台页面/前端）：{"limits": {"max_upload_mb": 300}}
          * 点分键（脚本/curl）：{"limits.max_upload_mb": 300}
        """
        flat: Dict[str, Any] = {}

        def walk(prefix: str, node: Any) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    if key.startswith("_") or not isinstance(key, str):
                        continue
                    walk(f"{prefix}.{key}" if prefix else key, value)
            else:
                flat[prefix] = node

        for key, value in (patch or {}).items():
            if not isinstance(key, str) or key.startswith("_"):
                continue
            if isinstance(value, dict) and "." not in key:
                walk(key, value)
            else:
                flat[key] = value

        applied: Dict[str, Any] = {}
        for dotted, value in flat.items():
            matched = next(
                (k for k in RUNTIME_EDITABLE if dotted == k or dotted.startswith(k + ".")),
                None,
            )
            if matched is None:
                continue

            if matched == "ocr.defaults" or matched.startswith("ocr.defaults."):
                # 逐项校验阈值参数的类型与范围
                target = self.raw.setdefault("ocr", {}).setdefault("defaults", {})
                sub = dotted[len("ocr.defaults"):].lstrip(".")
                key = sub or None
                if key is None and isinstance(value, dict):
                    for k2, v2 in value.items():
                        coerced = _coerce(target.get(k2), v2)
                        if coerced is not None:
                            target[k2] = coerced
                            applied.setdefault("ocr", {}).setdefault("defaults", {})[k2] = coerced
                    continue
                if key:
                    coerced = _coerce(target.get(key), value)
                    if coerced is not None:
                        target[key] = coerced
                        applied.setdefault("ocr", {}).setdefault("defaults", {})[key] = coerced
                continue

            # 枚举/范围校验：非法值直接报错（路由层转 400），绝不写进覆盖文件
            if matched == "ocr.device":
                text = str(value).strip().lower()
                if not _DEVICE_SPEC_RE.match(text):
                    raise ValueError(
                        f"非法设备配置：{value!r}；可用写法 auto / cpu / gpu / gpu:0 / xpu:0 / npu:0"
                    )
                coerced = text
            elif matched == "ocr.ocr_tier":
                text = str(value).strip().lower()
                if text not in ("tiny", "small", "medium"):
                    raise ValueError(f"非法 OCR 档位：{value!r}；只能是 tiny / small / medium")
                coerced = text
            elif matched == "ocr.gpu_mem_fraction":
                try:
                    number = float(value)
                except (TypeError, ValueError) as exc:
                    raise ValueError("gpu_mem_fraction 必须是 0~0.95 的数字") from exc
                if not 0 <= number <= 0.95:
                    raise ValueError("gpu_mem_fraction 必须在 0~0.95 之间（0 表示用 Paddle 默认）")
                coerced = number
            elif matched in ("ocr.text_rec_batch_size", "ocr.textline_batch_size"):
                try:
                    number = int(value)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"{matched} 必须是 1~256 的整数") from exc
                if not 1 <= number <= 256:
                    raise ValueError(f"{matched} 必须在 1~256 之间")
                coerced = number
            else:
                if isinstance(value, dict):
                    continue  # 该键本身是标量，忽略非法嵌套
                coerced = _coerce(self.get(matched), value)
            if coerced is None:
                continue
            self._set(dotted, coerced)
            node = applied
            parts = dotted.split(".")
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = coerced

        if applied:
            existing: Dict[str, Any] = {}
            if self._overrides_path.exists():
                try:
                    existing = json.loads(self._overrides_path.read_text("utf-8")) or {}
                except Exception:
                    existing = {}
            merged = _deep_merge(existing, applied)
            tmp = self._overrides_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self._overrides_path)
        return applied

    # ----------------------------------------------------------------- secrets
    def _ensure_secret(self) -> None:
        if self.get("server.session_secret"):
            self.session_secret = str(self.get("server.session_secret"))
            return
        secret_file = self.data_dir / ".session_secret"
        try:
            if secret_file.exists():
                self.session_secret = secret_file.read_text("utf-8").strip()
            if not getattr(self, "session_secret", ""):
                self.session_secret = secrets.token_urlsafe(48)
                secret_file.write_text(self.session_secret, encoding="utf-8")
                os.chmod(secret_file, 0o600)
        except OSError:
            self.session_secret = secrets.token_urlsafe(48)

    # ------------------------------------------------------------ convenience
    @property
    def tasks_dir(self) -> Path:
        return self.data_dir / "tasks"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"

    @property
    def official_models_dir(self) -> Path:
        return self.model_home / "official_models"

    @property
    def max_upload_bytes(self) -> int:
        return int(self.get("limits.max_upload_mb", 200)) * 1024 * 1024

    def public_dict(self) -> Dict[str, Any]:
        """给前端用的配置（不含任何凭据）。"""
        return {
            "version": self.get("version", "1.0.0"),
            "limits": self.get("limits", {}),
            "ocr": {
                "pipeline": self.get("ocr.pipeline"),
                "device": self.get("ocr.device"),
                "concurrency": self.get("ocr.concurrency"),
                "pdf_dpi": self.get("ocr.pdf_dpi"),
                "max_side_px": self.get("ocr.max_side_px"),
                "defaults": self.get("ocr.defaults", {}),
            },
            "indra": {
                "enabled": bool(self.get("indra.enabled", True)),
                "defaults": {
                    "math": bool(self.get("indra.math", False)),
                    "keep_furniture": bool(self.get("indra.keep_furniture", False)),
                },
            },
            "export": self.get("export", {}),
        }

    def admin_dict(self) -> Dict[str, Any]:
        data = copy.deepcopy(self.raw)
        data.get("server", {}).pop("admin_password", None)
        data.get("server", {}).pop("session_secret", None)
        data["server"]["admin_password_set"] = True
        return data


def apply_paddlex_env(settings: Settings) -> None:
    """把离线运行所需的 PaddleX 环境变量写入进程环境（必须在 import paddlex 前调用）。"""
    os.environ.setdefault("PADDLE_PDX_CACHE_HOME", str(settings.model_home))
    # 只用官方 BOS 源；离线时模型已存在本地，不会触发下载
    os.environ.setdefault("PADDLE_PDX_MODEL_SOURCE", "BOS")
    # 关键：关闭在线模型源连通性探测，否则离线环境启动会长时间卡住
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
    # 避免任何 HF / ModelScope 的隐式联网
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    os.environ.setdefault("MODELSCOPE_OFFLINE", "1")
    os.environ.setdefault("PADDLE_PDX_DISABLE_TELEMETRY", "True")
    # Indra OCR 模型目录（可选产线）：配置了就锁定到本地目录，避免隐式联网
    indra_dir = settings.get("indra.model_dir", "")
    if indra_dir:
        os.environ.setdefault("INDRA_OCR_MODEL_DIR", str(_abs_path(indra_dir)))
    # 显存池比例等必须在 import paddle 前落到环境变量（运行时 set_flags 只能兜底）
    try:
        from . import devices as _devices

        _devices.pre_import_env(settings)
    except Exception:  # noqa: BLE001
        pass

    cpu_threads = settings.get("ocr.cpu_threads", 0)
    if not cpu_threads:
        # Apple Silicon 没有 GPU 后端，多线程是唯一能提速的旋钮；给一个合理默认
        try:
            if sys.platform == "darwin" and platform.machine().lower() in ("arm64", "aarch64"):
                cpu_threads = min(8, os.cpu_count() or 4)
        except Exception:  # noqa: BLE001
            cpu_threads = 0
    if cpu_threads:
        os.environ.setdefault("OMP_NUM_THREADS", str(cpu_threads))
        os.environ.setdefault("FLAGS_omp_num_threads", str(cpu_threads))
    os.environ.setdefault("PYTHONUNBUFFERED", "1")



def _coerce(current: Any, value: Any) -> Any:
    """按现有值的类型做强校验，非法值返回 None（表示忽略）。"""
    if current is None:
        return value if isinstance(value, (bool, int, float, str)) else None
    try:
        if isinstance(current, bool):
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)
        if isinstance(current, int):
            if isinstance(value, bool):
                return None
            number = int(float(value))
            return number if number >= 0 else None
        if isinstance(current, float):
            return float(value)
        if isinstance(current, str):
            return str(value)
    except (TypeError, ValueError):
        return None
    return value

_settings: Optional[Settings] = None


def get_settings(config_path: Optional[str] = None) -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings(config_path)
    return _settings
