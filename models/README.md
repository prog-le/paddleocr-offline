# 模型目录说明

## 为什么模型不放进 Git 仓库

PaddleX 官方模型（四条产线全套约 2.9 GB / 21 个模型）体积大且是二进制权重，不适合进版本库。正确做法：

1. 在**有外网**的机器上执行 `python3 scripts/download_models.py`
2. 脚本把权重落到 `models/paddlex_home/official_models/<模型名>/`
3. 把整个 `models/` 目录拷到离线服务器（或打进镜像）

> 模型权重与 CPU 架构**无关**。在 macOS/ARM 机器上下载的权重，Linux x86_64 上可直接用，
> 反之亦然，可以只在有网的任意一台机器下载一次。

## 目录约定

```
models/
├── manifest.json                     # download_models.py 生成（名称/大小/摘要）
├── indra/                            # （可选）Indra OCR 权重，约 73 MB
└── paddlex_home/                     # 等价于 PADDLE_PDX_CACHE_HOME
    └── official_models/
        ├── PP-DocLayout_plus-L/
        ├── PP-DocBlockLayout/
        ├── PP-OCRv5_server_det/
        └── ...
```

## 各产线怎么下模型

```bash
# 1) 纯文字识别 · PP-OCRv6（默认 medium 档，约 133 MB）
python3 scripts/download_models.py --config config/pipelines/OCR.yaml
#    想用 small / tiny 档：先改 config.yaml 的 ocr.ocr_tier，再执行同一命令
#    （或临时用官内置 OCR：python3 scripts/download_models.py --pipelines OCR）

# 2) 通用版面解析 v3（精简，默认产线，约 1.9 GB）
python3 scripts/download_models.py --config config/pipelines/PP-StructureV3.yaml

# 3) 通用版面解析 v3（完整：区域检测 + 印章，共用大部分模型，增量约 150 MB）
#    注意：PP-DocBlockLayout 在官方 BOS 源现可下载（2026-11 实测），无需切源
python3 scripts/download_models.py --config config/pipelines/PP-StructureV3-full.yaml

# 4) 通用表格识别（专用表格产线；与上面共用版面/OCR/单元格检测，仅新增 SLANeXt_wireless）
python3 scripts/download_models.py --config config/pipelines/table_recognition_v2.yaml
```

四条产线全下 = **21 个模型 / 约 2.9 GB**（含 PP-OCRv5 与 PP-OCRv6 各一套 OCR 权重）。已下过的模型会自动跳过。
清单可用 `python3 scripts/download_models.py --manifest-only` 单独重建（只扫目录，不下载）。

> **PP-OCRv6 三档任选其一即可**，不必全下：
> `PP-OCRv6_medium_det` + `medium_rec`（133 MB）/ `small`（30 MB）/ `tiny`（6 MB）。
> 档位由 `ocr.ocr_tier` 决定，引擎会在建模时把两个 `model_name` 覆盖成对应档位。

> 模型源可选 `BOS`（默认）/ `ModelScope` / `HuggingFace` / `AIStudio`。
> 默认的 BOS 源在 2026-11 实测可覆盖本项目全部模型（含 `PP-DocBlockLayout`）；
> 若目标网络访问 BOS 不畅，再切 `PADDLE_PDX_MODEL_SOURCE=ModelScope`。

## 纯文字识别（PP-OCRv6）需要的模型

| 环节 | 模型 | 体积 | 说明 |
| --- | --- | --- | --- |
| 文本检测 | `PP-OCRv6_medium_det` | 59.4 MB | medium 档，PPLCNetV4 + RepLKFPN |
| 文本识别 | `PP-OCRv6_medium_rec` | 73.3 MB | medium 档，PPLCNetV4 + LightSVTR，50 语言 |
| 文本检测（小） | `PP-OCRv6_small_det` | 9.6 MB | `ocr.ocr_tier: small` |
| 文本识别（小） | `PP-OCRv6_small_rec` | 20.4 MB | `ocr.ocr_tier: small` |
| 文本检测（微） | `PP-OCRv6_tiny_det` | 1.9 MB | `ocr.ocr_tier: tiny`，不支持日文 |
| 文本识别（微） | `PP-OCRv6_tiny_rec` | 4.4 MB | `ocr.ocr_tier: tiny`，49 语言 |

- v6 是 **tiny/small/medium** 三档，**没有 v5 的 server/mobile 命名**；
- 单模型统一支持中/英/日 + 46 种拉丁语系；**阿拉伯文/西里尔文/天城文/韩文仍需 v5**；
- `OCR.yaml` 里只写了 medium 两个模型名，运行时由 `ocr.ocr_tier` 覆盖；
  切档位后如果对应权重没下载，首次加载会联网失败——离线环境请先补下载；
- 退回 v5：把 `OCR.yaml` 的 `model_name` 改成 `PP-OCRv5_server_det` / `PP-OCRv5_server_rec`，
  再执行 `python3 scripts/download_models.py --config config/pipelines/OCR.yaml`。

## PP-StructureV3 实际需要的模型

以 `paddlex==3.7.2` 的官方产线配置为准（可用
`python -m paddlex --get_pipeline_config PP-StructureV3` 随时核对）：

| 环节 | 模型 | 说明 |
| --- | --- | --- |
| 版面区域检测 | `PP-DocLayout_plus-L` | 标题/段落/表格/图片区域 |
| 版面块检测 | `PP-DocBlockLayout` | 阅读顺序相关块 |
| 区域检测（完整版） | `PP-DocBlockLayout` | 仅 `use_region_detection=true`；本项目默认关闭该开关以省显存与时间 |
| 图表转表格 | `PP-Chart2Table` | 仅 `use_chart_recognition=true`；约 2.2 GB，默认不启用 |
| 文档方向 | `PP-LCNet_x1_0_doc_ori` | 仅 `use_doc_orientation_classify=true` |
| 文档矫正 | `UVDoc` | 仅 `use_doc_unwarping=true` |
| 文本检测/识别 | `PP-OCRv5_server_det` + `PP-OCRv5_server_rec` | OCR 子产线 |
| 文本行方向 | `PP-LCNet_x1_0_textline_ori` | 仅 `use_textline_orientation=true` |
| 表格分类 | `PP-LCNet_x1_0_table_cls` | 有线/无线表判别 |
| 有线表结构 | `SLANeXt_wired` | |
| 无线表结构 | `SLANet_plus` | |
| 有线表单元格 | `RT-DETR-L_wired_table_cell_det` | |
| 无线表单元格 | `RT-DETR-L_wireless_table_cell_det` | |
| 公式识别 | `PP-FormulaNet_plus-L` | 仅 `use_formula_recognition=true`（体积大，建议按需下载） |
| 印章识别 | `PP-OCRv4_server_seal_det` | 仅 `use_seal_recognition=true` |

> **纯 OCR 产线（`config/pipelines/OCR.yaml`）** 使用
> `PP-OCRv6_medium_det` + `PP-OCRv6_medium_rec`（关闭 doc_ori/unwarp/textline_ori），
> 共约 133 MB，适合快速验证与纯文本场景。

只想要精简模型时：

```bash
python3 scripts/download_models.py --config config/pipelines/OCR.yaml   # PP-OCRv6，约 133 MB
python3 scripts/download_models.py --config config/pipelines/PP-StructureV3.yaml  # 约 1.5~2.5 GB
```

## 通用表格识别（table_recognition_v2）需要的模型

| 环节 | 模型 | 说明 |
| --- | --- | --- |
| 版面检测（定位表格） | `PP-DocLayout_plus-L` | 与版面解析产线共用 |
| 表格分类（有线/无线） | `PP-LCNet_x1_0_table_cls` | 6.6 MB |
| 有线表结构识别 | `SLANeXt_wired` | 351 MB |
| 无线表结构识别 | `SLANeXt_wireless` | 本产线**唯一新增**的模型 |
| 有线表单元格检测 | `RT-DETR-L_wired_table_cell_det` | 与版面解析共用 |
| 无线表单元格检测 | `RT-DETR-L_wireless_table_cell_det` | 与版面解析共用 |
| 文本检测 | `PP-OCRv5_server_det` | 与其它产线共用（官方默认是 v4_server_det） |
| 文本识别 | `PP-OCRv5_server_rec` | 与其它产线共用（官方默认是 v4_server_rec_doc） |
| 表格方向分类 | `PP-LCNet_x1_0_doc_ori` | 检测 90/180/270 度旋转 |
| 图像矫正 | `UVDoc` | 仅 `use_doc_unwarping=true` 时用到 |

> 本项目的 `config/pipelines/table_recognition_v2.yaml` 把版面检测与 OCR 子模块换成了
> 已在其它产线使用的 `PP-DocLayout_plus-L` / `PP-OCRv5`（官方默认是 `PP-DocLayout-L` /
> `PP-OCRv4`），好处是同一张图在不同产线下文字结果一致、且少占 ~400 MB 磁盘。
> 需要严格对齐官方默认时，把 yaml 里这两个 `model_name` 改回去再下载即可。

## Indra OCR（可选产线）需要的模型

Indra OCR 的权重不在 PaddleX 缓存里，来自 Hugging Face 仓库 `datalab-to/indra-ocr`
（tag `v1.1.0`，`det/rec/order*.onnx` + `meta.json` 等约 73 MB）：

```bash
# 有外网机器，先安装可选依赖
pip install -r requirements-indra.txt
# 下载到 models/indra（可用 INDRA_OCR_MODEL_DIR 覆盖输出目录）
python3 scripts/download_indra_models.py
# huggingface.co 直连不通时改用镜像
HF_ENDPOINT=https://hf-mirror.com python3 scripts/download_indra_models.py
# 需要公式识别（config.yaml 的 indra.math: true）时加 --math
python3 scripts/download_indra_models.py --math
```

> 默认下载约 73 MB（不含公式模型）；`--math` 会额外拉 5 个公式文件，合计约 92 MB。
> 离线环境开 `indra.math: true` 前必须先 `--math` 下载，否则加载产线会报缺少 math 模型。

> 脚本直接拉取 HF 仓库快照到目标目录，再用 `indra_ocr.load(path=…)` 校验一遍。
> **不要直接调 `indra_ocr.load(path=…)` 来下载**——它只检查目录里有没有权重，缺文件直接抛
> `ModelError`，永远不会往你给的路径里下载。

下载完成后把 `models/indra` 一起拷到离线服务器，并在 `config/config.yaml` 设置：

```yaml
indra:
  model_dir: models/indra
```

> 未安装 `indra-ocr` 时该产线不会注册，`models/indra` 为空也不影响 PaddleX 产线。
> 模型权重遵循 OpenRAIL-M，商用前请确认许可。

## GPU 与模型的关系

模型权重与设备**无关**：CPU / NVIDIA GPU 共用同一份 `official_models/`。
GPU 版只影响 Paddle 运行时（`paddlepaddle-gpu` 轮子 3.35 GB + `nvidia-*` CUDA 运行库约 3 GB），
因此模型目录不需要为 GPU 单独准备一份。

## 运行时如何找到模型

`backend/settings.py` 的 `apply_paddlex_env()` 会在 `import paddlex` **之前**设置：

| 环境变量 | 值 | 作用 |
| --- | --- | --- |
| `PADDLE_PDX_CACHE_HOME` | `<项目>/models/paddlex_home` | 模型缓存根目录 |
| `PADDLE_PDX_MODEL_SOURCE` | `BOS` | 仅官方源；离线时命中本地缓存 |
| `PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK` | `True` | **关闭在线连通性探测**，否则离线启动会卡住 |
| `HF_HUB_OFFLINE` / `TRANSFORMERS_OFFLINE` / `MODELSCOPE_OFFLINE` | `1` | 禁止任何隐式联网 |

## 校验

```bash
ls models/paddlex_home/official_models     # 目录清单
ls models/indra                            # （可选）Indra 权重
cat models/manifest.json                   # 名称/大小/摘要
./scripts/preflight.sh                     # 环境 + 模型 + 端口
.venv/bin/python scripts/smoke_test.py     # 真推理 + 四种导出（最可靠）
```

## 常见问题

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 报 `PP-StructureV3 requires additional dependencies` | 只装了 `paddlex`，没装 `paddlex[ocr]` 的额外依赖 | `pip install "paddlex[ocr]==3.7.2"`（requirements.txt 已包含） |
| 日志出现 `Downloading ...` 或启动卡 2 分钟 | 未用 `scripts/start.sh` 启动 / 模型缺失 | 用启动脚本；核对模型目录名 |
| `Model ... not found` | 目录名不匹配（多后缀、下划线差异） | 以 `manifest.json` 中的名称为准 |
| 识别结果为空 | 图像过小/过淡，或取错了结果字段 | 看 `data/tasks/<id>/raw/page-*.json`；调低检测阈值 |
