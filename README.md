# PaddleX OCR Web · 离线文字与表格识别站点

> 上传 **图片 / PDF** → PaddleX 产线 **OCR + 表格识别** → 网页预览 → 导出 **Word / Excel / TXT / Markdown**
> 可选接入 **Indra OCR**（轻量 CPU 模型）：80+ 语言、手写体、自动纠偏，与 PaddleX 产线并列选择。
> 支持 **x86_64 与 ARM64 (aarch64)**，支持 **本地原生部署** 与 **Docker 容器部署**，部署完成后 **运行期完全离线**。
> CPU 即可运行，**可选 NVIDIA GPU (CUDA) 加速**；Apple Silicon 以原生 arm64 CPU 运行。

---

## 1. 功能一览

| 能力 | 说明 |
| --- | --- |
| 文件输入 | PDF、JPG、PNG、BMP、TIFF、WEBP；支持多文件批量上传 |
| 识别 | PaddleX 集成产线，**任务级可选**：纯文字识别、通用版面解析 v3（精简/完整）、通用表格识别；可选接入 Indra OCR（轻量 CPU、80+ 语言、手写体） |
| 多产线 | `config/pipelines/*.yaml` 即产线，前端下拉框按批选择；各产线按需懒加载、互不影响 |
| 排版还原 | 按阅读顺序输出标题/段落/表格/图注；表格保留 `rowspan/colspan` 合并结构 |
| 预览 | 网页直接渲染文本与表格（真实表格样式，非纯文本） |
| 导出 | `.docx`（真实表格+合并单元格）、`.xlsx`（每表一个工作表+合并单元格）、`.txt`、`.md`（GFM 表格） |
| 任务 | 队列、进度、逐页进度、成功/失败提示、取消、重试、删除；大 PDF 分页识别 |
| 会话隔离 | 每个浏览器一个**签名 Cookie 会话**，只能看到自己上传的任务与结果；管理员后台可见全部 |
| 参数 | 页面可调：文本检测阈值、识别置信度、版面阈值、边长限制、各类子模块开关 |
| 复制 | 识别结果一键复制全文 |
| 管理后台 | 登录后可看服务状态、**各产线加载状态**、模型清单、队列、统计、任务明细（含会话标识）、任务日志、系统日志、在线改配置、暂停/恢复接单、预热/重载/**卸载产线** |
| 离线 | 模型、Python 依赖（wheel）、镜像全部本地化；运行期不访问外网 |
| 双架构 | Linux x86_64/ARM64 同一套代码 + 按架构分目录的 wheel + 单 Dockerfile（`TARGETARCH`） |
| 跨平台交付 | `scripts/build_deploy_package.sh --all` 一次生成 Linux amd64/arm64、Linux GPU、macOS arm64、Windows x64 离线包，自动处理 Paddle 版本 / wheel / 随包运行时的差异 |
| 运行设备 | `auto` / `cpu` / `gpu:0`：启动自动探测，页面与后台显示实际设备；GPU 可配显存池与识别 batch。**Apple Silicon 明确只用 CPU**（Paddle 无 Metal/MPS） |
| 免安装 Python | 交付包内自带**可重定位的 CPython 3.12.15**（x86_64 / aarch64），目标机不需要预装 python3；安装脚本自动解包到 `.runtime/python` 并据此建 venv |

**不做的事**：不接第三方云 OCR、不需要数据库、不需要登录即可识别（仅后台需登录）。
GPU 是**可选加速**，不是必需项——没有 NVIDIA 卡时同一套代码以 CPU 运行。

---

## 2. 目录结构

```
paddlex-ocr-web/
├── README.md                        # 本文档
├── requirements.txt                 # 运行时依赖（Linux 目标机，版本已锁定）
├── requirements-macos.txt           # macOS 本机联调用（paddle 3.3.1）
├── requirements-indra.txt           # 可选：Indra OCR 轻量产线依赖
├── .gitignore
├── .dockerignore
├── config/
│   ├── config.yaml                  # 唯一配置文件（端口/并发/阈值/限额/保留期）
│   └── pipelines/                   # 产线目录：一个 yaml = 一条可选产线（文件名即 id）
│       ├── OCR.yaml                 # 纯文字识别（PP-OCRv6，medium/small/tiny 可切）
│       ├── PP-StructureV3.yaml      # 通用版面解析 v3（精简，默认）
│       ├── PP-StructureV3-full.yaml # 通用版面解析 v3（完整：区域检测/公式/印章）
│       └── table_recognition_v2.yaml # 通用表格识别（专用表格产线）
├── backend/                         # FastAPI 后端
│   ├── main.py                      # 应用入口：路由挂载、静态资源、生命周期
│   ├── settings.py                  # 配置加载 + PaddleX 离线环境变量注入
│   ├── logging_setup.py             # 控制台 + logs/app.log 滚动日志
│   ├── storage.py                   # 任务目录 / 原子写 JSON / 过期清理
│   ├── pdfio.py                     # PDF 逐页渲染（PyMuPDF→pypdfium2→poppler 自动降级）
│   ├── devices.py                   # 设备探测（CPU/GPU/XPU/NPU、Apple Silicon 限制、显存）
│   ├── engine.py                    # 单条产线封装（实例池 + 结果归一化）
│   ├── indra_engine.py              # Indra OCR 适配层（可选产线，结果归一化）
│   ├── engine_pool.py               # 多产线路由（按任务所选产线懒加载）
│   ├── pipelines.py                 # 产线注册表（扫描 config/pipelines/*.yaml）
│   ├── mock_engine.py               # MOCK 引擎（无模型联调/自检）
│   ├── manager.py                   # 任务队列、进度、超时、取消、日志
│   ├── security.py                  # 管理后台会话与登录限流
│   ├── session.py                   # 前台匿名会话（签名 Cookie，任务隔离）
│   ├── tableparse.py                # HTML/GFM 表格 → 带合并信息的二维网格
│   ├── docmodel.py                  # 文档中间模型（预览与导出的唯一数据源）
│   ├── converters/                  # 四种导出
│   │   ├── to_docx.py  to_xlsx.py  to_txt.py  to_md.py
│   │   └── __init__.py              # export_document() 统一入口
│   └── routes/
│       ├── public.py                # 上传/任务/结果/导出/健康检查
│       └── admin.py                 # 后台登录/概览/任务/日志/配置/服务控制
├── frontend/                        # 原生 HTML + CSS + JS（零 CDN、零构建）
│   ├── index.html                   # 主页面
│   ├── admin.html                   # 后台管理页
│   └── assets/
│       ├── app.css  app.js  admin.js  favicon.svg
│       └── swagger/                 # 内置 swagger-ui（离线 /api/docs 用，不走 CDN）
├── scripts/                         # 部署与运维脚本
│   ├── env.sh                       # 公共函数（架构识别/配置读取/日志着色）
│   ├── preflight.sh                 # 环境自检
│   ├── install_dev.sh               # 开发机在线安装（macOS 用 3.3.1）
│   ├── download_python_runtime.sh   # 下载随包 CPython 3.12 运行时（Linux/macOS/Windows）
│   ├── verify_runtime.sh            # 校验运行时 sha256 / ELF 架构 / GLIBC 版本
│   ├── download_wheels.sh           # 依赖离线包预下载（Linux/macOS/Windows × CPU·GPU）
│   ├── verify_gpu_wheel.sh          # 校验 GPU 轮子（无需显卡）
│   ├── gpu_deps.py                  # 补齐/检查 CUDA 运行库依赖（nvidia-*/cuda-python）
│   ├── download_models.py           # 模型离线打包
│   ├── download_indra_models.py     # Indra OCR 模型离线打包（可选）
│   ├── build_deploy_package.sh      # 一键生成全平台部署包（推荐入口）
│   ├── make_offline_bundle.sh       # 生成 Linux 整包 tar.gz（--device gpu 生成 GPU 包）
│   ├── make_platform_bundle.sh      # 生成 macOS / Windows 整包
│   ├── install_offline.sh           # 离线安装依赖（Linux / macOS，建 venv）
│   ├── install_windows.ps1          # Windows 离线安装（PowerShell 5.1）
│   ├── start.sh / stop.sh / status.sh
│   ├── start_windows.ps1 / stop_windows.ps1  # Windows 启停
│   ├── smoke_test.py                # 端到端自检（模型+推理+四格式导出）
│   └── docker_health.py             # 容器健康检查
├── deploy/
│   └── systemd/paddlex-ocr-web.service  # systemd 单元示例（离线环境变量已配好）
├── tests/                           # 无需模型的自动化测试
│   ├── e2e.sh                       # 端到端接口测试（60 项，MOCK 引擎）
│   ├── test_devices.py              # 设备解析单测（29 项）
│   ├── test_gpu_deps.py             # GPU 依赖闭包单测（10 项）
│   ├── test_memguard.py             # 内存保护单测（18 项）
│   ├── test_tableparse.py           # 表格 HTML/GFM 解析单测（14 项）
│   ├── test_engine_utils.py         # 结果归一化单测（21 项）
│   ├── gen_assets.py                # 生成测试用图片/PDF
│   └── verify_exports.py            # 导出文件回读校验
├── docker/
│   ├── Dockerfile                   # CPU 版，单文件双架构（TARGETARCH）
│   ├── Dockerfile.gpu               # NVIDIA GPU 版（仅 amd64，装 paddlepaddle-gpu）
│   ├── docker-compose.yml
│   ├── docker-compose.gpu.yml       # GPU 覆盖层（device reservations，等价 --gpus all）
│   ├── .env.example
│   ├── entrypoint.sh
│   ├── build_image.sh               # 构建镜像
│   ├── export_image.sh              # 导出离线 tar.gz
│   └── import_and_start.sh          # 离线服务器导入并启动
├── models/
│   ├── README.md                    # 模型目录说明与校验方法
│   ├── manifest.json                # （生成）模型清单
│   └── paddlex_home/official_models/  # 模型权重落盘位置
├── offline/
│   ├── wheels/
│   │   ├── amd64/                   # x86_64 的 cp312 wheel
│   │   └── arm64/                   # aarch64 的 cp312 wheel
│   └── python/
│       ├── amd64/                   # cpython-3.12.x+<rel>-x86_64-...-install_only.tar.gz
│       └── arm64/                   # cpython-3.12.x+<rel>-aarch64-...-install_only.tar.gz
├── .runtime/                        # （安装时生成）解包后的随包 Python，可删后重装
├── data/                            # 运行期数据（自动创建，可随时清空）
│   ├── tasks/<task_id>/             # 每任务：meta.json / input / pages / raw / exports
│   ├── exports/                     # 导出缓存
│   └── runtime_overrides.json       # 后台在线改的配置
├── logs/
│   ├── app.log                      # 应用日志（滚动）
│   └── server.log                   # 后台启动时的标准输出
└── dist/                            # 离线交付包（tar.gz / 镜像 tar）
```

### 数据流

```
浏览器 ──multipart──▶ /api/tasks ──▶ 任务队列(ThreadPool) ──▶ 渲染(每页 JPEG)
                                                              │
                                                    PaddleX PP-StructureV3
                                                    （版面 → OCR → 表格结构）
                                                              │
                        ┌─────────────────────────────────────┘
                        ▼
              result.json（Doc → pages → blocks）
                        │
        ┌───────────────┼────────────────┬──────────────┐
        ▼               ▼                ▼              ▼
     网页预览        docx 导出        xlsx 导出     txt / md 导出
```

---

## 3. 版本与兼容矩阵

| 组件 | 版本 | 说明 |
| --- | --- | --- |
| Python | **3.12**（随包 **CPython 3.12.15**） | 本地部署用包内运行时，Docker 用 `python:3.12-slim-bookworm`；wheel 标签统一 `cp312`。兼容 3.10~3.13，但换版本须同时换运行时并重下 wheel |
| PaddlePaddle | **3.4.0 (CPU)** | 官方 CPU 源同时提供 `linux_x86_64` 与 `linux_aarch64` wheel |
| PaddlePaddle（macOS / Windows） | **3.3.1 (CPU)** | PyPI 无 3.4.0 轮子；macOS 仅 Apple Silicon（`macosx_11_0_arm64`），Windows 仅 x64（`win_amd64`）；macOS 为本地联调，Windows 为原生部署（`requirements-windows.txt`） |
| PaddlePaddle（NVIDIA GPU） | **3.4.0 (GPU)** | `paddlepaddle-gpu`，官方 `cu129`/`cu130` 源，仅 `linux_x86_64`；paddle 轮子约 **3.35 GB**；CUDA/cuDNN 运行库由 `nvidia-*` pip 包提供（另约 3 GB，随离线包分发） |
| PaddleX | **3.7.2** | `paddlex[ocr]`，含 OCR / 表格识别 / 版面分析所需依赖 |
| FastAPI / Uvicorn | 0.115+ / 0.30+ | 轻量 Web 层 |
| PDF 渲染 | PyMuPDF（首选）/ pypdfium2 / poppler | 三者依次降级，均为离线可用 wheel |
| 导出 | python-docx / openpyxl | — |

> **为什么 pin 这两个版本**：`paddlepaddle` 在 PyPI 上只发布 x86_64 轮子，ARM64 轮子只在
> Paddle 官方 CPU 源（`https://www.paddlepaddle.org.cn/packages/stable/cpu/`）提供，
> 且 3.4.0 是当前同时具备双架构轮子的稳定版本。改版本时请**同时**修改：
> `requirements.txt`、`docker/Dockerfile` 的 `ARG PYTHON_VERSION`、以及本节表格。
>
> **GPU 版驱动要求**：`paddlepaddle-gpu 3.4.0 (cu129)` 需要 NVIDIA 驱动 ≥ 575.57.08
> （或 570 系列的 forward-compat 包）；容器还需要 `nvidia-container-toolkit`。
> 驱动偏旧时改用 `--cuda cu126` 重新下载 wheel（3.4.0 未在 cu126 发布，会回退到 3.3.1，脚本会提示）。
>
> **版本回退**：若目标机器上 `paddlepaddle==3.4.0` 与 `paddlex==3.7.2` 出现兼容问题，
> 可整体退回 `paddlepaddle==3.3.1`（PyPI 提供 x86_64、官方 CPU 源提供 aarch64，
> 双架构同样可用），按 §6.1 重新下载 wheel 与模型即可，代码无需改动。

> **产线选择**：站点内置**四条** PaddleX 官方产线，按批选用（默认 `PP-StructureV3`），
> 另可选接入 **Indra OCR**（装在环境里才会出现），详见下一小节。`PP-StructureV3` 由「版面区域检测 + OCR 子产线 + 表格识别子产线」组成，
> 输出的 `parsing_res_list` 已按阅读顺序排好，最适合"整页还原"；
> 只要表格用 `table_recognition_v2`（表格还原度最高、不解析正文）；只要文字用 `OCR`（最快）。

### 可选产线（任务级选择）

下拉框为**固定顺序**（与 `ocr.pipeline` 无关，默认项只是被选中并标注「默认」）：

| # | 产线 id | 名称 | 能力 | 模型体积 | 单页耗时 | 适用 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `OCR` | 纯文字识别（**PP-OCRv6**） | 文本检测 + 识别，无表格结构；档位 tiny/small/medium 由 `ocr.ocr_tier` 控制 | 0.13~0.2 GB | 3~15 s | 只要文字、追求速度 |
| 2 | `PP-StructureV3`（默认） | 通用版面解析 v3（精简） | 版面检测 + OCR + 表格结构 + 公式 | ~1.9 GB | 30~120 s | 常规公文、报价单、发票 |
| 3 | `PP-StructureV3-full` | 通用版面解析 v3（完整） | 上述 + **区域检测** + **印章识别** + 文档方向/矫正 | ~2.0 GB | 40~180 s | 复杂版面、多栏、带红章/公式的正式文件 |
| 4 | `table_recognition_v2` | 通用表格识别 | **专用表格产线**：表格分类 + 结构识别（有线 `SLANeXt_wired` / 无线 `SLANeXt_wireless`）+ 单元格检测（RT-DETR）+ 按单元格重新 OCR | 与精简版共用，另加 ~30 MB | 40~150 s | **只要表格**（报表、对账单、票据明细），跳过正文解析，表格还原度最高 |
| 5 | `INDRA`（可选） | Indra OCR | Datalab 1M 参数 ONNX 模型：80+ 语言、手写体、任意角度页面；输出阅读顺序版面块与 HTML 表格 | ~76 MB | CPU 约 0.2~1 s | 轻量 CPU 场景、手写件、多语种；**不做单元格级表格还原** |

> **Indra OCR（可选产线）**：默认不安装，安装后自动注册到下拉框末尾。
> ```bash
> pip install -r requirements-indra.txt   # 或 pip install indra-ocr
> ```
> 模型默认从 Hugging Face 下载（约 76 MB），但本项目运行期强制离线，请先在联网机器执行
> `python3 scripts/download_indra_models.py`，再把 `models/indra` 拷走并设置
> `indra.model_dir: models/indra`。未安装时该产线完全不可见，PaddleX 产线不受影响。
> 模型权重遵循 OpenRAIL-M，商业使用前请确认许可。

#### 纯文字识别已升级到 PP-OCRv6

`config/pipelines/OCR.yaml` 显式锁定 **PP-OCRv6**（paddleocr 3.7 / paddlex 3.7.2 的默认 OCR 模型），
不再依赖"官方默认恰好是哪个版本"。三档模型可在线切换（后台「OCR 档位」）：

| 档位 | 检测模型 | 识别模型 | 体积 | 特点 |
| --- | --- | --- | --- | --- |
| `medium`（默认） | `PP-OCRv6_medium_det` | `PP-OCRv6_medium_rec` | ~133 MB | 精度最高（官方口径：检测 Hmean 86.2、识别 Avg Acc 83.2） |
| `small` | `PP-OCRv6_small_det` | `PP-OCRv6_small_rec` | ~30 MB | 精度/速度平衡，移动端取向 |
| `tiny` | `PP-OCRv6_tiny_det` | `PP-OCRv6_tiny_rec` | ~6 MB | 最快，**不支持日文**（49 种语言） |

**升级要点（和 PP-OCRv5 的差异）**：

- 档位命名从 `server` / `mobile` 改为 **`tiny` / `small` / `medium`**，v6 没有 `server` 后缀模型；
- 单模型统一支持中/英/日 + 46 种拉丁语系共 **50 种语言**，不再需要按语系换模型；
- **阿拉伯文、西里尔文、天城文、韩文等仍需 v5**：把 `OCR.yaml` 里两个 `model_name` 改回
  `PP-OCRv5_server_det` / `PP-OCRv5_server_rec`，再执行
  `python3 scripts/download_models.py --config config/pipelines/OCR.yaml`；
- 输出字段不变（`rec_texts` / `rec_scores` / `rec_polys` / `dt_polys`），本项目的结果解析无需改动；
- 官方指标基于内部评估集，**与 v5 不可直接对比**，迁移前请用自己的样本重测。

> **精简要表格怎么选？** `PP-StructureV3` 会顺带解析正文并做阅读顺序排版，适合"整页还原"；
> `table_recognition_v2` 把算力全放在表格上（表格方向分类、有线/无线分别建模、按单元格切分 OCR），
> 跨格断字与合并单元格更稳，但不输出正文段落。同一张报价单实测两者表格结构一致，
> 本产线对线框表/无框表混排更稳。

- 选择方式：前端「识别产线」下拉框（每批文件单独生效），或 API 的 `pipeline` 字段。
- 专用表格产线的输出就是表格块：网页预览为可视化表格，导出 xlsx 直接落单元格（含合并），
  txt/md 为 TSV/GFM 表。若整页压根没有表格，会自动退回该产线内部 OCR 的文本结果，不会空白。
- 各产线**按需加载**：同一进程里同时用过两条，内存会累加（精简 1.9G + 完整 2.0G）。
  内存紧张时用后台「服务控制 → 卸载」释放，或只保留一条产线。
- 新增自定义产线：把 PaddleX 产线 yaml 放进 `config/pipelines/`，文件名即 id，重启后自动出现在下拉框。
  建议在 yaml 头部写注释作为展示信息；产线类型按 `pipeline_name` 自动判定，
  也可用 `# kind:` 强制指定（`ocr` / `structure` / `table`，决定结果解析器与可调参数集）：
  ```yaml
  # label: 我的产线
  # description: 说明文字
  # kind: table          # 可选，通常自动识别
  pipeline_name: table_recognition_v2
  ```
  想调整在列表中的位置，改 `backend/pipelines.py` 的 `PIPELINE_ORDER`（未登记的产线排在末尾）。
- 想限制只暴露部分产线：`ocr.available_pipelines: [PP-StructureV3, OCR]`。

### 运行设备（CPU / NVIDIA GPU / Apple Silicon）

默认 `ocr.device: auto`：**有可用加速卡就用，否则 CPU**。设备在进程启动时探测一次，
前台顶部、`/api/health`、后台「服务信息」都会显示实际值；改配置后需在后台点「应用并重载」。

| 平台 | 支持情况 | 说明 |
| --- | --- | --- |
| Linux x86_64 + NVIDIA GPU | ✅ **CUDA 加速** | 装 GPU 版 Paddle（`paddlepaddle-gpu` + `nvidia-*` 运行库，均由离线包提供），宿主机装驱动 + `nvidia-container-toolkit`（容器场景）；`device: auto` 或 `gpu:0` |
| Linux arm64 | ✅ CPU | 官方 GPU 轮子**没有 aarch64**，ARM 服务器只能用 CPU（或自行用 NVIDIA NGC 镜像） |
| macOS Apple Silicon | ✅ 原生运行 / ❌ 无 GPU 后端 | PaddlePaddle 没有 Metal/MPS 后端，**只能用 CPU**；配置 `gpu`/`mps` 会自动降级并在页面/日志提示原因。Intel Mac 无可用轮子 |
| Windows x64 | ✅ 原生运行（CPU） | Paddle 3.3.1；需预装 **VC++ 2015-2022 x64**；Windows ARM64 无轮子 |
| Intel XPU / 华为 NPU | ⚠ 取决于 Paddle 编译 | `device: xpu:0` / `npu:0`；本项目未随包提供对应 wheel，需自行替换 Paddle 版本 |

#### 设备写法与切换

| 值 | 含义 |
| --- | --- |
| `auto`（默认） | 依次探测 GPU → XPU → NPU → CPU，用第一个可用的 |
| `cpu` | 强制 CPU（GPU 机器上排查显存问题时建议先用它） |
| `gpu` / `gpu:0` / `gpu:1` | 指定 NVIDIA 卡；多卡时每个进程/实例占一张 |
| `xpu:0` / `npu:0` / `mlu:0` | 其它 Paddle 设备（需对应编译版本） |

切换方式（三选一）：

1. 改 `config/config.yaml` 的 `ocr.device` 后重启；
2. 后台「服务控制 → 运行设备 → 应用并重载」（会自动 `PATCH /api/admin/config` + 重建模型实例）；
3. 环境变量 `PADDLEX_OCR_DEVICE=gpu:0`（容器/K8s 常用，不必改配置文件；
   若后台在线改过该项，以 `data/runtime_overrides.json` 为准）。

> 设备、OCR 档位、识别 batch 都属于**构造期配置**，只改配置不会立即生效——
> 必须重建产线实例。后台的「应用并重载」按钮会自动做这件事（`engine_pool.reset()`）。

#### 显存与批处理配置

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `ocr.gpu_mem_fraction` | `0.0` | Paddle 显存池占卡比例（0~0.95）。0 = 按需增长（推荐单实例） |
| `ocr.gpu_mem_limit_mb` | `0` | 便捷写法：按卡总显存换算成比例；`gpu_mem_fraction > 0` 时以 fraction 为准 |
| `ocr.text_rec_batch_size` | `6` | 文本识别 batch。**GPU 建议 16~32**，CPU 建议 1~8；显存不足优先调小它 |
| `ocr.textline_batch_size` | `6` | 文本行方向分类 batch（仅开启 `use_textline_orientation` 时生效） |
| `ocr.concurrency` | `1` | 每条产线的实例数；GPU 上每个实例各占一份显存与模型，12G 以下的卡建议保持 1 |
| `ocr.pdf_dpi` / `ocr.max_side_px` | `200` / `2400` | 像素级内存/显存的主要来源，降一档通常省 30%~50% |

> `gpu_mem_fraction` 是 Paddle 显存池的**预留比例，不是硬上限**：池满后仍会 OOM。
> 真正的保护是「识别 batch + 单页像素上限」；OOM 时引擎会返回可操作的错误提示，不会杀进程。

#### 在 NVIDIA GPU 上部署（本地原生）

> 前提：**打包不需要显卡**，只有"跑推理"需要。GPU 轮子约 6.3 GB（paddle 3.35 GB + CUDA 运行库 3 GB），
> 只发 **linux/amd64**；`offline/wheels/amd64` 与 `offline/wheels-gpu/amd64` 不能混放 Paddle。

```bash
# 1) 联网机器：下载 GPU 版 wheel 到独立目录（与 CPU 轮子共存）
./scripts/download_wheels.sh --arch amd64 --device gpu --cuda cu129 --out offline/wheels-gpu
./scripts/verify_gpu_wheel.sh --dir offline/wheels-gpu/amd64   # 平台标签/体积/CRC/CUDA 依赖闭包

# 2) 联网机器：打 GPU 包（必须带 --wheels-dir；否则会去默认目录找 CPU 轮子）
./scripts/make_offline_bundle.sh --arch amd64 --device gpu \
    --wheels-dir offline/wheels-gpu/amd64
#    → dist/paddlex-ocr-web-1.0.0-amd64-gpu.tar.gz（约 8.7 GB）

# 3) 目标机：确认驱动与卡（cu129 需 >= 575.57.08）
nvidia-smi

# 4) 目标机：安装（包内 wheel 就位，--device auto 也能识别；显式写更清楚）
tar xzf paddlex-ocr-web-1.0.0-amd64-gpu.tar.gz
cd paddlex-ocr-web-1.0.0-amd64-gpu
./scripts/install_offline.sh --device gpu

# 5) 目标机：验收（preflight 第 1 步查 glibc/驱动，第 2 步查设备是否真的是 gpu:0）
./scripts/preflight.sh
.venv/bin/python scripts/smoke_test.py    # 真实推理（含显存日志）

# 6) 启动
./scripts/start.sh --daemon
curl -s localhost:8000/api/health | python3 -c "import sys,json;print(json.load(sys.stdin)['device']['active'])"
```

几个容易踩的点：

- **`--wheels-dir` 只影响打包**；包内 wheel 固定落在 `offline/wheels/<arch>/`，目标机安装时不必再指定目录。
  若直接把源码目录拷到目标机（不走 tar 包），则安装时也要带 `--wheels-dir offline/wheels-gpu/amd64`。
- **没有 NVIDIA 驱动的机器上装 GPU 版不会失败**：`install_offline.sh` 会把 `import paddle` 的
  `libcuda.so.1` 失败降级为 `WARN`（打包机属正常现象），CUDA 能力检测留到带卡机器上做。
- wheel 里有 `manylinux_2_28` 的包（numpy / pillow / PyMuPDF 等），因此目标机 **glibc 需 ≥ 2.28**
  （Ubuntu 20.04+ / Debian 10+ / RHEL 8+）；随包 CPython 运行时本身只需 2.17。
- 显存不足优先调小 `ocr.text_rec_batch_size` / `ocr.pdf_dpi`；单卡 12 GB 以下建议 `ocr.concurrency: 1`。

任务日志（`data/tasks/<id>/task.log`）在 GPU 下会写显存基线，例如：

```
运行设备：gpu:0 · NVIDIA GeForce RTX 4090
显存基线：整卡 1024/24564MB 已用，本进程池 reserved=0MB
第 1 页完成 | 显存池 2680MB（峰值 2810MB），整卡 3704/24564MB
```

#### Apple Silicon：明确只用 CPU

PaddlePaddle 至今没有 Metal/MPS 后端（`paddle.device.is_compiled_with_cuda()` 恒为 False，
也没有 mps place），所以 macOS arm64 上：

- `device: auto` → 直接定 CPU，连探测都跳过；
- `device: gpu` / `mps` → 自动降级为 CPU，并在日志、`/api/health`、页面「设备」标签里
  给出原因（不是静默失败）；
- 提速手段只有 CPU 侧：调大 `ocr.cpu_threads`（默认 `min(8, 核数)`）、降低 `pdf_dpi` /
  `max_side_px`、纯文字场景选 `OCR` + `tiny/small` 档；
- 需要 GPU 加速请把服务部署到带 NVIDIA 卡的 Linux 主机（代码与配置完全一致，改 `device` 即可）。

---

## 4. 端口、路径、账号速查

| 项目 | 默认值 | 修改位置 |
| --- | --- | --- |
| 监听地址 | `0.0.0.0:8000` | `config/config.yaml` → `server.host/port`，或 `PADDLEX_OCR_PORT` |
| 管理后台 | `http://IP:8000/admin` | — |
| 管理员账号 | `admin` / `PaddleX@2026` | `server.admin_username/admin_password`、`PADDLEX_OCR_ADMIN_PASSWORD`、compose 的 `ADMIN_PASSWORD` |
| 模型目录 | `models/paddlex_home/official_models` | `PADDLEX_OCR_MODEL_HOME` |
| 数据目录 | `data/`（任务、结果、导出缓存） | `storage.data_dir`、`PADDLEX_OCR_DATA_DIR` |
| 日志 | `logs/app.log`、`logs/server.log`、`data/tasks/<id>/task.log` | `storage.log_dir`、`PADDLEX_OCR_LOG_DIR` |
| 依赖 wheel | `offline/wheels/amd64`、`offline/wheels/arm64` | — |
| 产线配置 | `config/pipelines/*.yaml`（文件名即产线 id） | 增删文件后重启生效；`ocr.available_pipelines` 可限定范围 |
| 随包运行时 | 包内 `offline/python/<arch>/*.tar.gz` → 安装后 `.runtime/python/` | `PADDLEX_OCR_RUNTIME_DIR` 可改解包位置 |

---

## 5. 在开发机（macOS / 已有 conda 环境）上先跑起来

部署目标是 Linux，但界面与链路可以本机验证。**注意 `requirements.txt` 里的
`paddlepaddle==3.4.0` 只有 Linux 轮子**，在 Mac 上直接装会报
`Could not find a version that satisfies the requirement paddlepaddle==3.4.0`。

```bash
# 方式一：脚本一键（推荐）
./scripts/install_dev.sh --mirror --with-mock-test

# 方式二：手动 venv
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-macos.txt -i https://mirrors.ustc.edu.cn/pypi/simple

# 方式三：装进已有 conda 环境（如 ocr）
conda activate ocr
pip install "paddlex[ocr]==3.7.2" paddlepaddle==3.3.1 \
            fastapi uvicorn python-multipart PyYAML==6.0.2 Pillow \
            pypdfium2 PyMuPDF python-docx openpyxl psutil
```

启动并验证：

```bash
# 只看界面（不加载模型，用 MOCK 数据，秒开）
PADDLEX_OCR_MOCK=1 ./scripts/start.sh

# 用真实产线（先下载模型，见 §6.1；PP-OCRv6 仅约 133 MB）
python3 scripts/download_models.py --config config/pipelines/OCR.yaml
PADDLEX_OCR_PIPELINE=OCR ./scripts/start.sh --port 8000

# 本机是 Apple Silicon：会以 CPU 运行（Paddle 无 Metal 后端），页面「设备」标签会写明
```

> 只要 `paddlex` 而没装 `paddlex[ocr]` 的额外依赖时，`PP-StructureV3` 会抛
> `requires additional dependencies`；`requirements*.txt` 里写的是 `paddlex[ocr]`，
> 用脚本安装不会踩到。

### 5.1 快速开始（按场景选入口）

| 场景 | 命令 |
| --- | --- |
| 我想先看界面（无需模型） | `./scripts/start.sh --mock` → <http://127.0.0.1:8000> |
| 本地离线部署 | 见 [§6](#6-本地离线部署步骤) |
| Docker 离线部署 | 见 [§7](#7-docker-离线部署步骤) |

---

## 6. 本地离线部署步骤

> 分两台机器的角色：
> **A. 联网机器**（准备依赖与模型） → **B. 离线服务器**（只安装和运行）
> 若服务器本身能联网，可直接在服务器上执行 A 的步骤。

> **目标机没有任何 Python 也可以**：交付包内自带 CPython 3.12.15 运行时，
> `install_offline.sh` 会把它解包到 `.runtime/python` 并用它创建 `.venv`，
> 全程不调用系统 `python3`（也不需要 `python3-venv`/编译工具）。

### 6.0 一键生成部署包（推荐）

需要覆盖多个操作系统/架构时，不要手工逐个跑下载与打包脚本，用总入口：

```bash
./scripts/build_deploy_package.sh --list-targets    # 查看平台矩阵与依赖文件
./scripts/build_deploy_package.sh                   # 只打当前构建机平台
./scripts/build_deploy_package.sh --all             # 4 个 CPU 平台
./scripts/build_deploy_package.sh --all --with-gpu  # 再加 Linux NVIDIA GPU
./scripts/build_deploy_package.sh --targets "linux-amd64,windows-amd64"
./scripts/build_deploy_package.sh --all --models-separate   # 模型单独一个包，避免重复 4 份
./scripts/build_deploy_package.sh --all --skip-download     # 复用已下载的 wheel/运行时
./scripts/build_deploy_package.sh --all --dry-run           # 只打印将执行的命令
```

平台矩阵（以 Paddle/PaddleX 上游实际发布的 wheel 为准）：

| 目标 id | 系统 | 架构 | 设备 | Paddle 版本 | 依赖文件 | 产物 |
| --- | --- | --- | --- | --- | --- | --- |
| `linux-amd64` | Linux | x86_64 | CPU | 3.4.0 | `requirements.txt` | `...-amd64.tar.gz` |
| `linux-arm64` | Linux | aarch64 | CPU | 3.4.0 | `requirements.txt` | `...-arm64.tar.gz` |
| `linux-amd64-gpu` | Linux | x86_64 | NVIDIA GPU | 3.4.0 | `requirements.txt` + GPU wheel | `...-amd64-gpu.tar.gz` |
| `macos-arm64` | macOS 11+ | Apple Silicon | CPU | 3.3.1 | `requirements-macos.txt` | `...-macos-arm64.tar.gz` |
| `windows-amd64` | Windows 10 1803+ | x64 | CPU | 3.3.1 | `requirements-windows.txt` | `...-windows-amd64.zip` |

明确不支持（上游没有轮子，脚本会直接报错而不是打出坏包）：

- **macOS Intel**：PyPI 只发布 `macosx_11_0_arm64`，请改用 `macos-arm64` 或 Docker；
- **Windows ARM64**：没有 `paddlepaddle` 轮子，请改用 `windows-amd64`；
- **Linux ARM64 + NVIDIA GPU**：GPU 轮子仅 `linux_x86_64`，ARM 请用 NVIDIA NGC 镜像。

产物统一放在 `dist/release-<version>/`：

```
paddlex-ocr-web-<ver>-<target>.tar.gz|.zip
models-paddlex_home-<ver>.tar.gz     # --models-separate 时生成，各平台共用
SHA256SUMS                           # sha256sum -c 可直接校验
release-manifest.json                # 平台/依赖/大小/校验值清单
DEPLOY_ALL.txt                       # 各平台安装命令速查
```

依赖一致性由脚本主动校验（这些是最常见的离线包失败原因）：

- wheel 的 `cp312` 标签必须与随包 CPython 3.12 一致；`make_offline_bundle.sh` 会在打包前比对；
- 同一目录**不能混放 CPU 版与 GPU 版 Paddle**（`paddlepaddle-*` 与 `paddlepaddle_gpu-*`），下载脚本会自动清理另一种；
- macOS / Windows 的 Paddle 固定为 3.3.1，Linux 为 3.4.0，版本差异已写入各自的 `requirements-*.txt`；
- Windows 目标机需预装 **Microsoft Visual C++ 2015-2022 Redistributable (x64)**，安装脚本会检测并提示；
- Windows 解压运行时依赖系统自带 `tar.exe`（Win10 1803+ 自带），安装脚本会在缺失时明确报错；
- 构建机只需要 `python3` + `curl` + `tar`；Windows 的 zip 与 macOS 的 tar.gz 都能在 Linux/macOS 构建机上交叉生成（`--skip-download` 复用已有物料）。

### 6.1 联网机器：准备离线物料

```bash
# 0) 构建机基础环境（构建机需要 python3 + curl；目标机不需要任何 Python）
python3 -V && uname -m

# 1) 下载随包 CPython 3.12 运行时（双架构约 116 MB）
./scripts/download_python_runtime.sh                # 或 --arch arm64 只下一种
./scripts/verify_runtime.sh                         # 校验 sha256 / ELF 架构 / GLIBC ≤ 2.17

# 2) 下载 Linux 离线 wheel（--python 必须与运行时一致：3.12）
./scripts/download_wheels.sh --arch arm64 --python 3.12      # CPU 版
./scripts/download_wheels.sh --arch amd64 --python 3.12      # CPU 版
#    NVIDIA GPU（仅 amd64；paddle 轮子 3.35 GB + CUDA 运行库约 3 GB）：
#    ./scripts/download_wheels.sh --arch amd64 --device gpu --cuda cu129 --out offline/wheels-gpu
#    注意：同一目录只能放一种 Paddle（脚本会自动清掉另一种）；分开目录可共存，
#    打包时用 --wheels-dir offline/wheels-gpu/amd64 指定来源。
#    国内网络可换镜像（示例）：
#    PYPI_INDEX=https://mirrors.ustc.edu.cn/pypi/simple PIP_TRUSTED_HOST=mirrors.ustc.edu.cn \
#      ./scripts/download_wheels.sh --arch arm64 --python 3.12

# 3) 下载模型（这一步需要本机能跑 paddlex，会真实推理一次做验证）
#    GPU 机器上同样执行：模型与设备无关，CPU/GPU 共用同一份权重
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
python3 scripts/download_models.py                  # 不带参数 = 全部产线（约 2.3 GB）
#    或按需单条：
#    python3 scripts/download_models.py --config config/pipelines/PP-StructureV3.yaml
#    python3 scripts/download_models.py --config config/pipelines/PP-StructureV3-full.yaml
#    python3 scripts/download_models.py --config config/pipelines/table_recognition_v2.yaml
#    python3 scripts/download_models.py --config config/pipelines/OCR.yaml   # PP-OCRv6
#    python3 scripts/download_models.py --pipelines OCR                     # 官方内置 OCR
#    可选 Indra OCR 模型（需先 pip install -r requirements-indra.txt）：
#    python3 scripts/download_indra_models.py
#    个别模型（如 PP-DocBlockLayout）BOS 源缺失，可切源：
#    PADDLE_PDX_MODEL_SOURCE=ModelScope python3 scripts/download_models.py --config ...

# 4) 打整包（代码 + 运行时 + wheel + 模型 + 脚本）
./scripts/make_offline_bundle.sh --arch arm64       # 或 amd64
# 产物：dist/paddlex-ocr-web-1.0.0-arm64.tar.gz
```

`download_models.py` 会生成 `models/manifest.json`，包含每个模型的名字/大小/摘要，
离线服务器可用它核对完整性。

> **包里不会有 macOS 的"垃圾文件"**。`make_offline_bundle.sh` 内置 31 条排除规则，覆盖：
> `.DS_Store` / `._*`（AppleDouble）/ `__MACOSX` / `.Spotlight-V100` / `.fseventsd` /
> `.com.apple.*`、`__pycache__` / `*.pyc` / `.pytest_cache` / `.mypy_cache`、`*.log` / `locks/`、
> 模型下载器的记账文件 `.msc` / `.mv` / `._tmp`、`.gitkeep` / `.gitattributes`、
> 临时 `.requirements-*.txt`。
> 三处共用同一份清单：复制时 `rsync --exclude`、复制后 `find -prune` 兜底、打包时 `tar --exclude`；
> 生成后还会对**包内清单**做终检——命中任何一条就报错并打印具体路径。
> 另外 tar 用 `COPYFILE_DISABLE=1` 打出（同时显式排除 `._*`/`.AppleDouble`），并会探测
> `--no-xattrs` 是否可用：macOS 的 bsdtar 支持时启用，GNU tar 不支持则自动跳过，避免误报。
> 想看"到底排除了什么"，先跑 `./scripts/make_offline_bundle.sh ... --dry-run`，
> 第 1 步会打印「垃圾体检：源目录中有 N 个无关文件，打包时会全部排除」并列出前 15 条。

> **关于 `config/pipelines/PP-StructureV3.yaml`**：这是官方产线配置的副本，唯一差异是
> `use_region_detection: False`。原因：该开关会额外加载 `PP-DocBlockLayout`，而该模型在
> 官方 BOS 源上无法下载，且它只影响"区域二次切分"，关闭后表格识别与阅读顺序不受影响。
> 引擎会自动优先使用 `config/pipelines/<产线名>.yaml`（可用 `ocr.pipeline_config` 指定其它路径）。
> 模型清单与各模型用途见 `models/README.md`。

### 6.2 离线服务器：安装

```bash
# 1) 拷贝并解压（U 盘 / scp / 内网传输均可）
tar xzf paddlex-ocr-web-1.0.0-arm64.tar.gz
cd paddlex-ocr-web-1.0.0-arm64

# 2) 离线安装（无需系统 python：会先解包包内 CPython 3.12，再建 .venv 并离线装依赖）
./scripts/install_offline.sh

#    常用参数：
#    --prepare-only                 只解包运行时 + 建 venv（不装依赖，便于排障）
#    --check-only                   只做运行时/wheel/版本匹配检查，不改动任何文件
#    --device gpu                   装 paddlepaddle-gpu（默认 auto：检测到 GPU 轮子就用）
#    --wheels-dir offline/wheels-gpu/amd64
#                                   wheel 不在默认位置时指定（直接拷源码目录而非 tar 包时常用）
#    --system-python                改用系统 python3（需 3.12，且 --python 3.12 的 wheel）
#    --venv /opt/ocr-venv           把虚拟环境放到别处
#    --runtime /path/xxx.tar.gz     指定其它运行时压缩包

# 3) 环境自检（架构 / 运行时 / 依赖 / 模型 / 产线 / 权限 / 端口）
./scripts/preflight.sh

# 4) 端到端自检（真实推理 + 四种导出，最可靠）
.venv/bin/python scripts/smoke_test.py            # 默认产线
.venv/bin/python scripts/smoke_test.py --all      # 四条产线逐条验证（慢，但最完整）
```

> **macOS / Windows 目标机**：macOS（Apple Silicon）解压后执行同一条 `./scripts/install_offline.sh`
>（脚本会自动识别 `macos-arm64`，Paddle 为 3.3.1）；Windows 解压 zip 后执行
>`powershell -ExecutionPolicy Bypass -File scripts\install_windows.ps1`，再用
>`scripts\start_windows.ps1` / `scripts\stop_windows.ps1` 启停（详见包内 `DEPLOY_QUICKSTART.txt`）。

安装完成后目录里会多出两样东西（都可安全删除后重装）：

| 路径 | 内容 |
| --- | --- |
| `.runtime/python/` | 包内 CPython 3.12.15 解包结果（约 200 MB），`.venv` 由它创建 |
| `.runtime_env` | 记录解释器路径 / Python 版本 / 安装时间，`preflight.sh` 会读 |

> **不要移动项目目录后继续用旧 `.venv`**：venv 里的 python 是指向 `.runtime/python` 的
> 绝对路径软链。换目录后执行 `rm -rf .venv .runtime_env && ./scripts/install_offline.sh` 重建即可。

> 若第 2 步报 `wheel 架构不匹配`：说明拷错了架构的 wheel 目录，或服务器的
> `uname -m` 是 `aarch64` 而包是 `amd64`。重新在对应架构机器上执行
> `./scripts/download_wheels.sh --arch <arch>` 再打包即可。

### 6.3 离线服务器：启动与运维

```bash
# 前台启动（调试用，Ctrl+C 停止）
./scripts/start.sh

# 后台启动（推荐）
./scripts/start.sh --daemon
./scripts/status.sh                 # 进程 + /api/health
tail -f logs/app.log                # 应用日志
./scripts/stop.sh                   # 停止
```

访问：

- 识别页面：`http://<服务器IP>:8000`
- 管理后台：`http://<服务器IP>:8000/admin`

首次启动会加载模型（20~90 秒，取决于磁盘与 CPU），期间 `/api/health` 返回
`503` + `issues`，属正常预热；后台可点「预热模型」查看状态。

启动脚本会依次找解释器：`$VENV_DIR` → `.venv` → `.venv_path` 记录 → 系统 `python3`；
只要执行过 `install_offline.sh`，用的就是包内运行时，**与系统是否装 Python 无关**。
若检测到运行时但没建 venv，会明确提示先跑安装脚本。

启动后确认设备是否如预期（**这一步别省**）：

```bash
curl -s localhost:8000/api/health | python3 -m json.tool | head -30
#  "device": {
#    "configured": "auto",
#    "active": {"device": "gpu:0", "kind": "gpu", "name": "NVIDIA GeForce RTX 4090"},
#    "capabilities": {"cuda": true, ...}
#  }
```

- `kind=cpu` 但机器有卡 → 看 §10 FAQ「装了 GPU 版仍然用 CPU」；
- `fallback=true` 且 `reason` 提到 Metal/MPS → 你在 Apple Silicon 上，属正常行为。

### 6.4 用 systemd 托管（可选，推荐生产使用）

项目自带单元文件 `deploy/systemd/paddlex-ocr-web.service`（离线环境变量、`--workers 1`、
`MemoryMax` 注释都已写好），**不要手抄**，直接用现成的再改路径/账号：

```bash
sudo useradd -r -s /sbin/nologin -d /opt/paddlex-ocr-web ocr   # 专用低权限账号（可选）
sudo cp deploy/systemd/paddlex-ocr-web.service /etc/systemd/system/
sudo vi /etc/systemd/system/paddlex-ocr-web.service   # 改 User/Group/WorkingDirectory/端口
sudo systemctl daemon-reload && sudo systemctl enable --now paddlex-ocr-web
sudo systemctl status paddlex-ocr-web
sudo journalctl -u paddlex-ocr-web -f
```

单元文件里的关键项（照抄容易漏，列出来便于核对）：

| 项 | 值 | 为什么 |
| --- | --- | --- |
| `ExecStart` | `.venv/bin/python -m uvicorn backend.main:app --workers 1` | **必须 1 worker**：任务队列与产线实例都在进程内，多 worker 会队列分裂、内存翻倍 |
| `PADDLE_PDX_CACHE_HOME` | `<项目>/models/paddlex_home` | 指到随包分发的模型目录，否则会去用户目录找 |
| `PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK` | `True` | 跳过模型源探测，否则离线启动会卡 1~2 分钟 |
| `HF_HUB_OFFLINE` / `TRANSFORMERS_OFFLINE` / `MODELSCOPE_OFFLINE` | `1` | 双保险，禁止子库联网 |
| `Restart` | `always` | 推理 OOM 被 kill 后自动拉起 |

> 提高吞吐请改 `ocr.concurrency`（见 §9），不要加 worker。

### 6.5 配置说明（`config/config.yaml`）

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `server.host` / `port` | `0.0.0.0` / `8000` | 监听地址；也可用 `PADDLEX_OCR_HOST/PORT` |
| `server.admin_password` | `PaddleX@2026` | **上线前务必修改** |
| `server.session_ttl_sec` | `7200` | 后台登录有效期 |
| `server.client_session_ttl_hours` | `24` | 前台会话 Cookie 有效期（换/清 Cookie 后旧任务不可见） |
| `server.client_cookie_name` | `pdx_sid` | 前台会话 Cookie 名 |
| `server.root_path` | `""` | 反向代理子路径，如 `/ocr` |
| `limits.max_upload_mb` | `200` | 单文件大小上限 |
| `limits.max_files_per_batch` | `20` | 单次批量上传文件数 |
| `limits.max_pdf_pages` | `100` | 单 PDF 最大页数，超出直接报错 |
| `limits.max_image_pixels` | `50000000` | 单页像素上限，超出等比缩小 |
| `limits.task_timeout_sec` | `7200` | 单任务超时（页边界检查）；密集扫描件约 120 s/页，按机器能力调 |
| `limits.queue_max_size` | `100` | 等待队列上限，超出返回 429/400 |
| `ocr.pipeline` | `PP-StructureV3` | **默认产线**，可选 `OCR` / `PP-StructureV3-full` / `table_recognition_v2`（不影响下拉框顺序） |
| `ocr.available_pipelines` | `[]` | 白名单，留空=自动发现全部；例 `[PP-StructureV3, OCR]` |
| `ocr.device` | `auto` | 运行设备：`auto` / `cpu` / `gpu` / `gpu:0` / `xpu:0` / `npu:0`；改完需重载产线 |
| `ocr.gpu_mem_fraction` | `0.0` | Paddle 显存池占卡比例（0~0.95，0=按需增长） |
| `ocr.gpu_mem_limit_mb` | `0` | 按 MB 指定显存池上限（换算成 fraction；fraction>0 时以它为准） |
| `ocr.ocr_tier` | `medium` | PP-OCRv6 档位：`medium` / `small` / `tiny` |
| `ocr.text_rec_batch_size` | `6` | 文本识别 batch（GPU 可 16~32，CPU 建议 1~8） |
| `ocr.textline_batch_size` | `6` | 文本行方向分类 batch |
| `ocr.cpu_threads` | `0` | CPU 线程数；0=自动（Apple Silicon 默认 `min(8, 核数)`） |
| `ocr.concurrency` | `1` | 并发推理实例数（每实例 1.5~3 GB，CPU 建议 1） |
| `ocr.pdf_dpi` | `200` | PDF 渲染 DPI（150 快 / 300 准） |
| `ocr.max_side_px` | `2400` | 送入推理前长边上限 |
| `ocr.defaults.*` | 见文件 | 识别阈值与子模块开关（前端可临时覆盖） |
| `indra.enabled` | `true` | 是否启用 Indra OCR 产线（需已安装 `indra-ocr`，改完重启生效） |
| `indra.model_dir` | `""` | Indra 模型目录；留空用包内缓存，离线部署建议 `models/indra` |
| `indra.threads` | `0` | ONNX Runtime 线程数，0=使用 indra-ocr 默认值（4） |
| `indra.math` / `indra.keep_furniture` | `false` | 公式转 LaTeX / 保留页眉页脚默认值（前端可临时覆盖） |
| `storage.retention_hours` | `24` | 结果保留时长，到期自动清理 |
| `storage.keep_page_images` | `true` | 是否保留每页渲染图（排查用） |
| `export.include_noise_blocks` | `false` | 是否导出页眉/页脚/页码 |

环境变量覆盖（容器/多实例常用）：`PADDLEX_OCR_PORT`、`PADDLEX_OCR_ADMIN_PASSWORD`、
`PADDLEX_OCR_DATA_DIR`、`PADDLEX_OCR_LOG_DIR`、`PADDLEX_OCR_CONCURRENCY`、
`PADDLEX_OCR_PDF_DPI`、`PADDLEX_OCR_MODEL_HOME`、`PADDLEX_OCR_LOG_LEVEL`、
`PADDLEX_OCR_DEVICE`、`PADDLEX_OCR_GPU_MEM_MB`、`PADDLEX_OCR_GPU_MEM_FRACTION`、
`PADDLEX_OCR_TEXT_REC_BATCH`、`PADDLEX_OCR_TIER`。

后台「运行时配置」可**在线修改**白名单字段（限量、阈值、保留期、**设备/档位/批大小**等），
写入 `data/runtime_overrides.json`；`ocr.pipeline` / `ocr.concurrency` 需改文件后重启。
设备、`ocr_tier`、batch 这类构造期参数改完必须点后台「应用并重载」（或重启），否则不会生效。

---

### 6.6 没有 NVIDIA 显卡，怎么打包与验收？

**先分清两件事：打包不需要显卡，只有"GPU 推理"才需要。**

| 步骤 | 需要显卡吗 | 需要什么 |
| --- | --- | --- |
| 下载 GPU 轮子（paddle 3.35 GB + CUDA 依赖约 3 GB，共约 6.3 GB） | ❌ 不需要 | 任意联网机器（x86_64 Linux / macOS 均可交叉下载） |
| 打 GPU 离线包（`-amd64-gpu.tar.gz`） | ❌ 不需要 | 磁盘 ≥ 20 GB 空闲（轮子 6.3 GB + 包约 8~9 GB） |
| 构建 GPU Docker 镜像 | ❌ 不需要 | x86_64 构建机（或 Mac + buildx/QEMU，慢但可行） |
| 安装到目标机 | ❌ 不需要 | 目标机有 NVIDIA 卡即可，安装过程不跑推理 |
| **验证 GPU 真的在跑** | ✅ **需要** | 目标机，或临时租用的 GPU 云主机 |

所以在你的 Mac 上就能完成打包，命令与 CPU 版只差 `--device gpu`：

```bash
# 1) 下载 GPU 轮子（macOS 也能跑：脚本会带 --platform linux_x86_64 交叉下载）
#    --out 把它放到独立目录，CPU 轮子保持原样，两套可共存
#    脚本会自动补下 nvidia-*/cuda-python 等 Linux 专属依赖（约 3 GB）
./scripts/download_wheels.sh --arch amd64 --device gpu --cuda cu129 --out offline/wheels-gpu

# 2) 校验轮子（不需要显卡）：平台标签 / 体积 / zip 完整性 / CUDA 依赖是否齐全 / sha256
./scripts/verify_gpu_wheel.sh --dir offline/wheels-gpu/amd64

# 3) 打 GPU 离线包（输出名带 -gpu，避免与 CPU 包混淆）
./scripts/make_offline_bundle.sh --arch amd64 --device gpu \
    --wheels-dir offline/wheels-gpu/amd64
#    → dist/paddlex-ocr-web-1.0.0-amd64-gpu.tar.gz

# 4) 可选：构建 GPU 镜像（同样不需要显卡；自动使用 offline/wheels-gpu）
./docker/build_image.sh --arch amd64 --device gpu --wheels-dir offline/wheels-gpu/amd64
./docker/export_image.sh --arch amd64 --gpu
```

> 一个 `offline/wheels/amd64` 目录只能放一种 Paddle：`download_wheels.sh --device gpu`
> 会自动删除 CPU 版残留（反之亦然），`make_offline_bundle.sh` 检测到混放会直接报错。
> 因此**CPU 包与 GPU 包要分两次准备**（脚本会提示；已打好的 CPU 包不受影响）。

#### 没有显卡时，怎么确认 GPU 包是好的？

按可信度从高到低，三种做法：

1. **目标机验收（推荐，零额外成本）**
   把 `-amd64-gpu.tar.gz` 传到目标 GPU 服务器，执行：
   ```bash
   ./scripts/install_offline.sh --device gpu
   ./scripts/preflight.sh                    # 第 2 步应显示 "设备 gpu:0 · NVIDIA ..."
   .venv/bin/python scripts/smoke_test.py    # 真实推理，日志会写显存基线
   ```
   三条都通过就说明 GPU 路径可用；失败时 `logs/app.log` 会给驱动/CUDA 的专项提示。
2. **租 1 小时 GPU 云主机做验收**
   在 AutoDL / 阿里云 / 腾讯云 / AWS 上开一台最便宜的 T4 实例（按小时计费），
   把包 scp 上去跑上面三条命令，确认后立即释放。适合"必须先证明能用再交付"的场景。
3. **完全无卡时的静态校验（只能到"可安装"级别）**
   `./scripts/verify_gpu_wheel.sh` 会检查平台标签、3.35 GB 体积、zip CRC、`nvidia-*` 依赖是否齐全
   （含 `cuda-python -> cuda-bindings` 这类无平台标记的传递依赖，并按 `==`/`~=` 比对版本），
   再配合 `install_offline.sh --device gpu` 能验证"装得上、导得进"；
   但 **`paddle.device.cuda.device_count()` 仍会是 0**，无法证明推理可用——这一步绕不过去。

#### 如果你根本没有 GPU 目标机

那就**不要打 GPU 包**，直接用现有 CPU 包即可：

- 代码里的 GPU 支持是"配置项"而非硬依赖：`ocr.device: auto` 在没有卡时自动走 CPU；
- 将来有了 GPU 机器，只需在该机器上换装 GPU 轮子（或直接把 `-amd64-gpu` 包传过去），
  代码、模型、前端都不用改；
- 现在已打好的两个 CPU 包（`arm64` / `amd64`）可直接交付。

## 7. Docker 离线部署步骤

> 构建机需要网络（拉基础镜像 + apt 依赖）。**离线服务器只需 `docker load`**。

### 7.1 构建机：构建双架构镜像

```bash
# 前置：模型与 wheel 已就绪（同 §6.1 的 1、2 步）
ls offline/wheels/amd64 | head
ls models/paddlex_home/official_models

# 方式一（推荐）：两种架构分别构建
./docker/build_image.sh --arch amd64
./docker/build_image.sh --arch arm64     # 需 buildx + QEMU，或在 arm64 机器上执行

# 方式二：一次构建两个架构（同样产出两个 tag）
./docker/build_image.sh --arch all
```

- 同架构构建：直接 `docker build`，无需 QEMU。
- 交叉构建：需要 `docker buildx` 与 QEMU：
  `docker run --privileged --rm tonistiigi/binfmt --install all`
- 产物 tag：`paddlex-ocr-web:1.0.0-amd64` / `paddlex-ocr-web:1.0.0-arm64`
  （多架构 manifest 无法 `docker save`，因此离线交付采用「每架构一个 tar」）

#### GPU 镜像（NVIDIA，仅 amd64）

```bash
# 构建机需先下好 GPU 轮子（paddle 3.35 GB + CUDA 依赖约 3 GB）
PYPI_INDEX=https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple \
  ./scripts/download_wheels.sh --arch amd64 --device gpu --cuda cu129 --out offline/wheels-gpu
# build_image 会自动优先使用 offline/wheels-gpu/<arch>；也可显式指定：
./docker/build_image.sh --arch amd64 --device gpu --wheels-dir offline/wheels-gpu/amd64
# tag: paddlex-ocr-web:1.0.0-amd64-gpu
./docker/export_image.sh --arch amd64 --gpu     # → dist/…-amd64-gpu.image.tar.gz
```

GPU 镜像与 CPU 镜像的差异只有「装 `paddlepaddle-gpu` 而不是 `paddlepaddle`」，
其余完全一致（模型内置、`--no-index` 离线安装、非 root 运行、健康检查）。

> **构建阶段不做 `import paddle`**：GPU 版 Paddle 导入时 dlopen `libcuda.so.1`，
> 而 `docker build` 的容器没有宿主驱动，必定报 `libcuda.so.1: cannot open shared object file`。
> 镜像里只用 `importlib.metadata` 校验安装结果，CUDA 可用性在运行时验证。
>
> **运行必须带显卡**：GPU 镜像即使是 `ocr.device=cpu`，导入 Paddle 也需要 `libcuda.so.1`，
> 所以容器必须 `--gpus all`（compose 的 device reservations），
> 或至少挂载宿主驱动库；没有卡的机器请用 CPU 镜像。

运行机必须满足：NVIDIA 驱动 ≥ 575.57.08 + `nvidia-container-toolkit`；
启动时用 GPU 覆盖层（等价 `--gpus all`）：

```bash
cd docker
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d
docker exec paddlex-ocr-web nvidia-smi -L       # 容器内应能看到卡
docker logs paddlex-ocr-web | head -20          # 会打印「NVIDIA GPU: ... / 驱动版本」
```

镜像内已包含：Python 3.12 基础环境、Python 依赖、PaddleX、全部模型、前端静态资源、脚本。
（镜像自带 `python:3.12-slim-bookworm`，与本地部署的随包 CPython 3.12 共用同一套 cp312 wheel；
因此 `offline/python/` 不会进镜像，构建上下文也不受影响。）
容器启动时 `PADDLE_PDX_CACHE_HOME=/app/models/paddlex_home`，**不会联网下载**。

### 7.2 构建机：导出离线镜像包

```bash
./docker/export_image.sh --arch amd64      # → dist/paddlex-ocr-web-1.0.0-amd64.image.tar.gz
./docker/export_image.sh --arch arm64      # → dist/…-arm64.image.tar.gz
# 同时生成 .sha256 校验文件
```

### 7.3 离线服务器：导入并启动

```bash
# 1) 传输后校验 + 导入 + 配置 + 启动 + 健康检查，一条命令完成
./docker/import_and_start.sh \
    --tar /opt/paddlex-ocr-web-1.0.0-arm64.image.tar.gz \
    --port 8000 \
    --admin-password 'YourStrongPassword'

# GPU 版（镜像 tag 带 -gpu，启动要叠加 GPU 覆盖层）
docker load -i paddlex-ocr-web-1.0.0-amd64-gpu.image.tar.gz
cd docker && docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d

# 或分步执行
docker load -i paddlex-ocr-web-1.0.0-arm64.image.tar.gz
cd docker && cp .env.example .env
vi .env          # IMAGE / HOST_PORT / ADMIN_PASSWORD / MEM_LIMIT
docker compose up -d
docker compose ps
docker logs -f paddlex-ocr-web
```

`import_and_start.sh` 会自动完成：

1. `sha256` 校验（存在 `.sha256` 时）
2. `docker load` 并解析出真实镜像 tag
3. 校验镜像架构与主机架构是否一致（不一致明确告警）
4. 生成/更新 `docker/.env`（`IMAGE`、`HOST_PORT`、`ADMIN_PASSWORD`）
5. `docker compose up -d --no-build`
6. 轮询 `/api/health` 直到就绪（最多约 2 分钟）

### 7.4 容器运维

```bash
cd docker
docker compose ps                       # 状态 / 健康
docker compose logs -f --tail 200       # 日志
docker compose restart                  # 重启
docker compose down                     # 停止并移除容器（数据在宿主机 data/ 不受影响）
docker compose pull                     # 离线环境下不要执行
```

- 数据持久化：`../data`、`../logs` 挂载到宿主机（`.env` 中 `DATA_DIR`/`LOG_DIR`）。
- 容器以 uid `10001` 运行，若宿主机目录权限不足：
  `sudo chown -R 10001:10001 data logs`
- 内存上限：`.env` 的 `MEM_LIMIT`（默认 **8g**）。产线**按需加载**：单产线 6g 起，
  混用两条（如精简 + 表格）10g 起，四条都用过且未卸载会累加到 12g 以上；不够时用后台
  「服务控制 → 卸载」释放，或调大 `MEM_LIMIT`。

#### 容器跑一会儿就“自己重启”？先确认是不是 OOM

这是本项目最常见的容器故障：**任务跑到一半内存超过 `mem_limit`，被内核 OOM kill，
`restart: unless-stopped` 又把容器拉起来**。`docker ps` 的表现就是 `CREATED 4 minutes ago`
但 `Up About a minute`。

```bash
# 1) 一眼确认（OOMKilled=true、ExitCode=137 即为内存超限被杀）
docker inspect paddlex-ocr-web \
  --format 'OOMKilled={{.State.OOMKilled}} ExitCode={{.State.ExitCode}} Restarts={{.RestartCount}}'

# 2) 看被杀前后在做什么
docker logs --tail 200 paddlex-ocr-web

# 3) 复现时盯内存曲线
docker stats --no-stream paddlex-ocr-web
```

容器日志与任务日志里现在会带内存数据，可直接对照：

```
[信息] 内存上限: 8192 MB  已用: 900 MB          # entrypoint 启动时打印
内存基线：RSS 1620MB / 限额 6144MB / 容器已用 2100MB / 可用 4044MB
第 1 页完成 | RSS 3480MB / 限额 6144MB / 容器已用 3900MB / 可用 2244MB   ← 峰值
```

**按顺序这样解决**（前两条基本能覆盖所有情况）：

| # | 动作 | 说明 |
| --- | --- | --- |
| 1 | `docker/.env` 里 `MEM_LIMIT=12g`，再 `docker compose up -d` | 最直接。混用多条产线时尤其需要 |
| 2 | 降单页像素：`ocr.pdf_dpi: 150`、`ocr.max_side_px: 1600`、`limits.max_image_pixels: 20000000` | 峰值内存大致随像素线性下降，能省 30%~50% |
| 3 | 后台「服务控制 → 卸载」不用的产线 | 四条产线全加载约占 10~12G |
| 4 | 把大 PDF 拆成几份分别识别 | 同时也能规避超时 |

> 从设计上已经“兜底”了：单页推理前会检查剩余内存（`limits.min_free_mem_mb`，默认 600MB），
> 不够就让**该任务**失败并给出上面的建议，而不是把容器打死。所以即使你不调参，
> 也应该是“任务失败 + 明确的报错”，而不是“服务莫名重启”。若升级后仍出现 OOM kill，
> 说明连“加载模型”这一步就超限了 —— 直接调大 `MEM_LIMIT`。

---

## 8. 接口说明（便于集成）

> **在线文档：`http://<服务器IP>:8000/api/docs`（Swagger UI），`/api/openapi.json` 是原始 spec。**
> 完全离线可用：swagger-ui 的 `swagger-ui-bundle.js`（1.6 MB）与 `swagger-ui.css`（180 KB）
> 已内置在 `frontend/assets/swagger/`，由后端用本地 URL 渲染（见 `backend/main.py` 的 `/api/docs`）。
> FastAPI 自带的文档页固定从 `cdn.jsdelivr.net` 拉这两个文件，在内网环境会 404、页面一片空白，
> 所以本项目把 `docs_url` 关掉后自行注册了离线版；升级 swagger-ui 见该目录下的 `VERSION`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/tasks` | multipart：`files`（可多个）+ `params`（JSON 字符串）+ `pipeline`（产线 id，可空）；返回 `batch_id` 与任务列表 |
| `GET` | `/api/tasks?status=&limit=&keyword=` | 任务列表 + 队列统计 |
| `GET` | `/api/tasks/{id}` | 单任务状态/进度/错误 |
| `GET` | `/api/tasks/{id}/result` | 规范化识别结果（pages → blocks，含表格网格） |
| `GET` | `/api/tasks/{id}/text` | 纯文本（一键复制用） |
| `GET` | `/api/tasks/{id}/export?fmt=docx\|xlsx\|txt\|md` | 下载导出文件（`noise=1` 含页眉页脚） |
| `POST` | `/api/tasks/{id}/cancel` | 取消（当前页结束后生效） |
| `DELETE` | `/api/tasks/{id}` | 删除任务与结果 |
| `GET` | `/api/meta` | 前端配置：限额、默认参数、支持格式、**可用产线列表**、会话标识 |
| `GET` | `/api/health` | 健康检查（`200` 正常 / `503` 降级，含 `issues`） |
| `POST` | `/api/admin/login` | 后台登录（表单 JSON），成功后 Set-Cookie |
| `GET` | `/api/admin/overview` | 服务状态、模型清单、统计、系统信息 |
| `GET` | `/api/admin/tasks` | 全量任务（含错误详情） |
| `GET` | `/api/admin/tasks/{id}/log` | 单任务日志 |
| `GET` | `/api/admin/logs?file=app.log&lines=500` | 服务日志 |
| `PATCH` | `/api/admin/config` | 在线改白名单配置 |
| `POST` | `/api/admin/service/{pause\|resume\|warmup\|reload\|unload}?pipeline=<id>` | 服务控制；`warmup/reload/unload` 可指定产线（留空为默认产线），`unload` 用于释放某条产线的内存 |
| `POST` | `/api/admin/maintenance/cleanup` | 立即清理过期任务 |

### 会话 Cookie（前台接口必须带）

前台接口按**浏览器会话**隔离数据：首次访问任意接口时服务端下发
`pdx_sid=<sid>.<HMAC>` 的 HttpOnly Cookie，之后上传的任务都归属于该会话。
所以用脚本/curl 调用时必须**保存并回传 Cookie**，否则每次请求都是新会话（看不到刚上传的任务）：

```bash
# 正确姿势：-c 保存、-b 回传
curl -c jar.txt -b jar.txt -F "files=@invoice.pdf" http://127.0.0.1:8000/api/tasks
curl -c jar.txt -b jar.txt http://127.0.0.1:8000/api/tasks
```

浏览器里不需要任何额外操作（Cookie 自动携带）。管理员接口走 `pdx_admin` Cookie，
可以看到全部会话的任务。

示例：

```bash
# 注意带 Cookie（-c 保存 / -b 回传），否则每次都是新会话
curl -c jar.txt -b jar.txt \
     -F "files=@invoice.pdf" -F 'files=@scan.png' \
     -F 'params={"use_table_recognition":true,"text_det_box_thresh":0.5}' \
     -F 'pipeline=PP-StructureV3' \
     http://127.0.0.1:8000/api/tasks

# 用专用表格产线识别一份报表
curl -c jar.txt -b jar.txt -F "files=@report.pdf" -F 'pipeline=table_recognition_v2' \
     http://127.0.0.1:8000/api/tasks

curl -c jar.txt -b jar.txt -OJ "http://127.0.0.1:8000/api/tasks/<id>/export?fmt=xlsx"
```

---

## 9. 异常处理、超时与资源限制

| 场景 | 系统行为 | 可调项 |
| --- | --- | --- |
| 超大文件 | 上传即中断并删除半截文件，返回 413 | `limits.max_upload_mb` |
| 文件伪装（改名） | 按文件头嗅探真实类型，非图片/PDF 直接拒绝 | — |
| PDF 加密/损坏 | 任务失败并给出可读原因（不重试） | — |
| PDF 页数超限 | 任务失败，提示拆分或调大上限 | `limits.max_pdf_pages` |
| 单页渲染失败 | 该页记入 `warnings` 并继续后续页；仅一页时任务失败 | — |
| 识别超时 | 页边界检查，超时任务标记失败并保留已完成页 | `limits.task_timeout_sec` |
| 图像过大 | 渲染后按长边/像素上限等比缩小再推理 | `ocr.max_side_px`、`limits.max_image_pixels` |
| 内存不足（容器） | **单页推理前检查余量**：不足则让该任务失败并给出调参建议，不会把整个容器拖到 OOM kill（否则表现为“服务自动重启”） | `limits.min_free_mem_mb`（默认 600）、容器 `MEM_LIMIT`、`ocr.pdf_dpi`、`ocr.max_side_px` |
| 显存不足（GPU） | 捕获 `CUDA out of memory`，任务失败并给出 5 条可操作建议（调小 batch / 像素 / 限流 / 卸载产线 / 改 CPU），**进程不退出** | `ocr.text_rec_batch_size`、`ocr.gpu_mem_fraction`、`ocr.pdf_dpi` |
| 配了 GPU 但用不了 | 服务仍可启动：`device_error` 在加载产线时报可读错误，`/api/health` 返回 degraded；后台可改回 `auto`/`cpu` 并重载 | `ocr.device` |
| 队列打满 | 新任务返回「队列已满」，不排队 | `limits.queue_max_size` |
| 服务重启 | 未完成任务标记为失败并提示重试（不会残留 running） | — |
| 结果过期 | 后台线程按小时清理 `data/tasks/*` | `storage.retention_hours` |
| 推理报错排查 | 每页原始 JSON 落 `data/tasks/<id>/raw/`，每任务日志落 `task.log` | `storage.keep_raw_json` |

**性能参考（CPU 实测）**：

| 产线 | 单页 A4@200dpi | 说明 |
| --- | --- | --- |
| `OCR`（纯文本） | 3~15 秒 | Apple M 系列 / 8 核 x86 量级 |
| `PP-StructureV3`（含表格结构+单元格检测） | **30~120 秒** | 取决于版面密度：1 页简单报价单 48 秒；官方公文扫描件实测 120 秒/页 |
| `PP-StructureV3-full`（+区域检测/印章/矫正） | **40~180 秒** | 多出的子产线各自还要跑一遍，按需选 |
| `table_recognition_v2`（专用表格） | **40~150 秒** | 表格方向分类 + 有线/无线分别建模 + 按单元格重 OCR，比精简版略慢但更稳 |

**GPU 参考（单卡，关闭公式/印章）**：`OCR`（PP-OCRv6 medium）约 0.3~1 s/页（含 PDF 渲染约 1~2 s）；
`PP-StructureV3` 约 3~10 s/页。实际取决于卡型（T4 / 4090 / A10 差异明显）与版面密度，
请以 `data/tasks/<id>/task.log` 的逐页耗时为准。

> 100 页带表格的扫描件 ≈ 1~3 小时（CPU）。默认 `limits.task_timeout_sec: 7200`（2 小时）
> 大约覆盖 60 页密集扫描；页数上限 `max_pdf_pages: 100` 是保护性软限制，
> **请按机器实际能力调整**：
> 只做纯文本就切 `ocr.pipeline: OCR`；需要表格则把超时调大到 7200 并限制页数。
> 降低 `pdf_dpi`（150）与 `max_side_px`（1600）可再省 20%~40% 时间。

**调优建议**

1. 只要文字不要表格：产线选 `OCR`（或改 `ocr.pipeline: OCR`），速度约提升 2~3 倍。
   只要表格不要正文：选 `table_recognition_v2`，比精简版略慢但合并单元格/跨格断字更稳。
2. 扫描件质量差：`pdf_dpi: 300`，页面参数里降低「文本检测框阈值」。
3. 需要公式：页面勾选「公式识别」（显著变慢）。
4. 吞吐优先：`ocr.concurrency: 2`，同时保证内存 ≥ 8G（每实例 1.5~3G）。
5. GPU 优先：`ocr.device: auto`，`text_rec_batch_size: 16~32`，`pdf_dpi` 可保持 200；
   显存吃紧时先降 batch，再降 `gpu_mem_fraction`，最后才降 DPI。
6. 内存吃紧：产线是**按需加载**的，用过不想留的可在后台「服务控制 → 卸载」（`unload`）释放；
   四条产线全加载约占 10~12G，建议只保留常用的 1~2 条。

---

## 10. 常见问题

**Q1：启动很快但任务全部失败，日志提示模型缺失？**
`models/paddlex_home/official_models` 为空或目录名不对。在联网机器执行
`python3 scripts/download_models.py`，把 `models/` 整个拷过来；后台「模型清单」可核对。

**Q2：日志里出现 `Downloading`、启动卡住 2 分钟？**
说明 `PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK` 未生效（不要直接 `python backend/main.py`，
用 `scripts/start.sh`；容器用镜像默认环境变量）。该变量在导入 paddlex 前注入。

**Q3：macOS 上 `pip install -r requirements.txt` 报 paddlepaddle==3.4.0 找不到？**
该版本只有 Linux 轮子。Mac 请用 `pip install -r requirements-macos.txt`（3.3.1）或
`./scripts/install_dev.sh`；生产部署仍走 Linux + 3.4.0。

**Q4：目标机完全没有 python3，能装吗？**
可以。交付包内自带 CPython 3.12.15 运行时（`offline/python/<arch>/cpython-3.12.*-install_only.tar.gz`），
`./scripts/install_offline.sh` 会把它解包到 `.runtime/python` 并用它创建 `.venv`，全程不调用系统 python。
装完用 `./scripts/preflight.sh` 确认「运行时来源：随包」即可。若包里没有运行时（打包时用了
`--skip-runtime`），安装脚本会明确报错并提示去联网机执行 `./scripts/download_python_runtime.sh`。

**Q5：报 `PP-StructureV3 requires additional dependencies`？**
只装了 `paddlex` 基础包。执行 `pip install "paddlex[ocr]==3.7.2"`（本项目 requirements 已含）。

**Q6：ARM64 安装时 `paddlepaddle` 找不到可用版本？**
PyPI 没有 aarch64 轮子，`download_wheels.sh` 会追加 Paddle 官方 CPU 源。
若仍失败，直接在该 ARM 机器上执行 `./scripts/download_wheels.sh --arch arm64 --native`。

**Q7：导出 Word 里中文变成方块？**
导出时已设置 `w:eastAsia=宋体`；极简系统若缺中文字体，打开文档的客户端会回退字体，
不影响文字内容。服务器端无需安装中文字体。

**Q8：表格结构不对/合并单元格错乱？**
表格结构由 PaddleX 表格识别子产线给出。可尝试：提高 `pdf_dpi`、确保整表在视野内、
页面参数中关闭「弯曲矫正」以外的增强项；复杂无线表建议导出 Excel 后微调。

**Q9：识别过程中进度条会卡住十几秒？**
CPU 推理时单进程会短暂占满 GIL，接口偶发数秒级延迟（实测多为 10ms，个别阶段约 10~18s），
任务本身不受影响，进度会随后一次性补上。若不能接受，可把 `ocr.concurrency` 保持 1，
并把服务放在独立进程/容器中，避免与其他服务抢 CPU。

**Q10：识别很慢？**
CPU 推理是算力瓶颈。优先：① `pdf_dpi` 降到 150；② 关闭公式/印章；③ `pipeline: OCR`；
④ 增加 CPU 核数或 `ocr.concurrency`（需足够内存）。

**Q11：Docker 里任务跑一会儿，服务就自己重启了？**
几乎都是**容器被 OOM kill**：任务推理时内存超过 compose 里的 `mem_limit`，内核杀掉进程，
`restart: unless-stopped` 又把容器拉起来 —— 从外面看就是“自动重启”。确认方法：

```bash
docker inspect paddlex-ocr-web \
  --format 'OOMKilled={{.State.OOMKilled}} ExitCode={{.State.ExitCode}}'
```

退出码对照（先分清是哪一类）：

| OOMKilled | ExitCode | 含义 | 处理 |
| --- | --- | --- | --- |
| `true` | `137` | 内存超限被内核杀（**最常见**） | 调大 `MEM_LIMIT`、降 `pdf_dpi` / `max_side_px` |
| `false` | `137` | 被 `docker stop`/`kill` 或 `docker compose down` 杀 | 检查是否有外部脚本/面板在重启它 |
| `false` | `139` | 原生库段错误（PDF 渲染/推理库） | 换 `pdf_dpi`/`max_side_px` 复现，留下容器日志 |
| `false` | `143` | 收到 SIGTERM 正常退出 | 同上，多半是有人执行了 stop/restart |

处理：把 `docker/.env` 的 `MEM_LIMIT` 调到 `12g`（单产线 6g 起、混用两条 10g 起），
或把 `ocr.pdf_dpi` 降到 150、`ocr.max_side_px` 降到 1600。
容器日志会打印内存上限与每页 RSS，后台「系统信息」也会显示容器口径的内存。

**Q11.5：装了 GPU 版，为什么还是用 CPU？**

按顺序查：

1. `/api/health` → `device.capabilities.cuda` 是否为 `true`。false 说明装的是 CPU 版
   `paddlepaddle`（`pip show paddlepaddle-gpu` 为空），需重装：
   `./scripts/install_offline.sh --device gpu`；
2. `device.active.kind` 是否为 `cpu` 且 `configured=gpu`：说明卡不可见。裸机看
   `nvidia-smi`；容器看 `docker exec <容器> nvidia-smi -L`，为空就是缺 `--gpus all`
   或 `nvidia-container-toolkit`；
3. 驱动版本低于 575.57.08 时 cu129 轮子会初始化失败，`logs/app.log` 里会有
   `cuda`/`cudnn` 相关报错；
4. 配置改完没重载：设备是构造期参数，后台点「应用并重载」。

**Q11.6：我手上没有 NVIDIA 显卡，怎么打包 GPU 版？**

打包不需要显卡：`download_wheels.sh --device gpu`（可交叉下载）、`make_offline_bundle.sh
--device gpu`、`docker/build_image.sh --device gpu` 都能在没有卡的机器上完成，
只需要网络和磁盘。只有"验证 GPU 真的在跑"必须上带卡机器。
完整步骤与三种验收方式见 §6.6。

> 下载 GPU 轮子时注意两点：
> 1. `pip` 在 macOS 上会按当前平台跳过 `platform_system == "Linux"` 的依赖，
>    所以脚本会用 `gpu_deps.py` 反复补下 `nvidia-*` / `cuda-python`（约 3 GB），
>    直到闭包完整；缺一个都会在容器里报 `nvidia-cuda-nvrtc-cu12==... not found`。
> 2. **USTC 镜像缺少 CUDA 12.9.x 的 nvidia 包**，请用清华镜像：
>    `PYPI_INDEX=https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple`

**Q11.7：GPU 显存怎么配才不 OOM？**

先看任务日志里的「显存基线 / 每页显存池峰值」：

- 峰值接近整卡可用显存 → 调小 `ocr.text_rec_batch_size`（32 → 16 → 8）；
- 多任务并发 → `ocr.concurrency` 保持 1（每个实例一套模型 + 一份显存池）；
- 想留出余量给别的进程 → `ocr.gpu_mem_fraction: 0.6`（预留比例，不是硬上限）；
- 还是不够就降 `ocr.pdf_dpi` / `max_side_px`，或把这条产线换成 `OCR`。

**Q11.8：Apple Silicon 能用 GPU/Metal 加速吗？**

不能。PaddlePaddle 没有 Metal/MPS 后端（macOS 版只编译了 CPU 内核），
所以本项目在 Apple Silicon 上**只以 CPU 运行**：`device: auto` 直接定 CPU，
配 `gpu`/`mps` 会降级并在页面与日志里说明原因。
可用的提速手段是 `ocr.cpu_threads`（默认 `min(8, 核数)`）、降低 DPI、选 `OCR` + tiny/small 档。

**Q11.9：PP-OCRv6 与 PP-OCRv5 有什么要注意的？**

- v6 档位是 `tiny/small/medium`，**没有 server/mobile**；`ocr.ocr_tier` 切换即可；
- v6 单模型支持 50 种语言，但**阿拉伯文/西里尔文/天城文/韩文仍需 v5**；
- 输出字段与 v5 一致（`rec_texts/rec_scores/rec_polys`），前端与导出无需改动；
- 官方指标基于内部评估集，不可与 v5 直接对比，迁移前请用自有样本重测。

**Q12：该选哪条产线？**
常规公文/发票/报价单用默认的 `PP-StructureV3`（精简）即可；版面复杂、多栏、带红章或公式、
且能接受更慢时选 `PP-StructureV3-full`；只要文字不要表格就选 `OCR`；
**只要表格、不要正文**（报表/对账单/票据明细）选 `通用表格识别（table_recognition_v2）`——
它对合并单元格与跨格断字最稳，导出 xlsx 的合并区域最准。
同一批文件可以换产线重跑（重新上传或后台「重试」时改配置）。

**Q13：通用表格识别为什么只出了表格、没有正文？**
这是该产线的设计：`table_recognition_v2` 只输出表格（`table_res_list`），不解析段落与阅读顺序。
要"整页还原"请用 `PP-StructureV3`；要"表格最准"用本产线。整页无表格时会自动退回该产线内部
OCR 的文本结果，不会出现空白页。

**Q14：换个浏览器（或清了 Cookie）后，之前识别的任务不见了？**
这是设计行为：前台上传的文件与结果按浏览器会话隔离，`pdx_sid` Cookie 失效后即不可见。
数据没有被删（仍在 `data/tasks/` 与保留期内），管理员在后台「任务管理」里能看到并导出。
如需多台机器共用同一批结果，请让管理员代为导出，或在服务端把 `data/` 目录共享出来。

**Q15：后台 401？**
会话默认 2 小时（`server.session_ttl_sec`），服务重启后需重新登录。密码见
`config/config.yaml` 或 compose 的 `ADMIN_PASSWORD`。

**Q16：能否放到 Nginx 子路径 `/ocr`？**
可以：`server.root_path: /ocr`，Nginx `location /ocr/ { proxy_pass http://127.0.0.1:8000/; }`，
注意同时透传 `Host` 与 `X-Forwarded-Proto`。

**Q17：如何彻底清空历史？**
停服后删除 `data/tasks/*`、`data/exports/*`；或后台点「清理过期任务」。

---

## 11. 安全与合规提示

- 本站**无公网认证**（仅内网使用）。请用防火墙限制来源网段，或置于内网网关之后。
- **前台数据按浏览器会话隔离**：`pdx_sid` 为随机 sid + `session_secret` 的 HMAC 签名，
  伪造/篡改会被识别并重新签发；任务只对创建它的会话可见，管理员后台可见全部。
  注意这只是"防误看"，不是身份认证——拿到对方 Cookie 的人就能看到对方任务，
  因此仍需内网访问控制。
- 会话有效期 `server.client_session_ttl_hours`（默认 24h，与任务保留期一致）；
  清除 Cookie / 换浏览器 / 换设备后看不到旧任务，任务文件仍按保留期存在，
  需要找回请联系管理员在后台查看或导出。
- 生产环境务必修改 `admin_password`；建议通过环境变量注入而非写入配置文件。
- 识别文件属于敏感数据：`data/` 会短期留存原件与结果，请按 `retention_hours` 定期清理，
  或将 `data` 放在加密分区。
- 导出文件名做了字符过滤，接口不返回服务器绝对路径。
- 服务默认只读模型目录、只写 `data/` 与 `logs/`，容器内以非 root 用户运行。

---

## 12. 上线前检查清单（打包 → 部署验收）

### 12.1 打包机（有网）上必须确认

| # | 检查项 | 命令 / 说明 |
| --- | --- | --- |
| 1 | **wheel 架构正确** | `ls offline/wheels/arm64 \| head`，目标机 `uname -m` 必须对应（aarch64→arm64、x86_64→amd64） |
| 2 | **wheel 的 Python 版本与目标机一致** | 文件名里的 `cp310`/`cp312` 必须匹配目标机 `python3 -V`。不一致会在安装时报 `Python 版本与 wheel 不匹配`（脚本已做拦截） |
| 3 | **随包运行时齐全** | `./scripts/verify_runtime.sh` 全绿（sha256 / ELF 架构 / GLIBC ≤ 2.17）；`offline/python/<arch>/*.tar.gz` 存在（arm64 51 MB / amd64 65 MB） |
| 4 | **运行时与 wheel 同版本** | 运行时 3.12 ↔ wheel 标签 `cp312`；不一致时打包脚本会告警，安装脚本会直接拦截 |
| 5 | **模型齐全** | `python3 scripts/download_models.py --manifest-only` 重建清单；四条产线合计 17 个模型 / 约 2.3 GB |
| 6 | **打包干跑** | `./scripts/make_offline_bundle.sh --arch <arch> --dry-run` 先看计划 |
| 7 | **正式打包** | `./scripts/make_offline_bundle.sh --arch <arch>`，末尾会自检包内必需文件（缺一个就报错退出） |
| 8 | 包内不含垃圾 | 脚本已排除 `.DS_Store` / `__pycache__` / `*.pyc` / `models/*/locks` / `data` / `logs` |
| 9 | 修改管理员口令 | `config/config.yaml` → `server.admin_password`（启动日志会持续告警默认口令） |

### 12.2 目标服务器上验收

```bash
tar xzf paddlex-ocr-web-1.0.0-<arch>.tar.gz && cd paddlex-ocr-web-1.0.0-<arch>

./scripts/verify_runtime.sh               # ① 运行时完整性：sha256 / ELF 架构 / GLIBC ≤ 2.17
./scripts/install_offline.sh --check-only # ② 安装前预检：运行时 3.12 ↔ wheel cp312 是否匹配
./scripts/install_offline.sh              # ③ 解包运行时 + 建 venv + 离线装依赖（无需系统 python）
./scripts/preflight.sh                    # ④ 架构/运行时/依赖/模型/产线/权限/端口 自检
.venv/bin/python scripts/smoke_test.py    # ⑤ 真实推理 + 四种导出（最关键）
./scripts/start.sh --daemon               # ⑥ 启动
./scripts/status.sh                       # ⑦ 健康检查（exit 0 才算正常）
```

`preflight.sh` 的第 2 步会明确打印「运行时来源：随包（…/.runtime/python）」，
这就是「目标机没装 Python 也在用包内解释器」的确认点。

> **GPU 包（`-amd64-gpu.tar.gz`）多两步**：安装要写 `./scripts/install_offline.sh --device gpu`；
> `preflight.sh` 第 1 步会检查 **glibc ≥ 2.28** 与 **NVIDIA 驱动 ≥ 575.57.08（cu129）**，
> 第 2 步应显示「设备 gpu:0 · <卡名>」。若在**无卡机器**上安装，`import paddle` 会因
> `libcuda.so.1` 失败——脚本会降级为 `WARN` 并继续（打包机属正常），CUDA 能力检测留到带卡机器做。

`smoke_test.py` 输出「自检全部通过」即代表：产线可加载、能推理、docx/xlsx/txt/md 都能生成并回读。
想四条产线都验证一遍（会更慢，模型逐条加载再释放）：

```bash
.venv/bin/python scripts/smoke_test.py --all
# 只验某一条：
.venv/bin/python scripts/smoke_test.py --pipeline table_recognition_v2
```

### 12.3 资源与端口

| 项目 | 建议 | 说明 |
| --- | --- | --- |
| 内存 | ≥ 8 GB（单产线）；混用多条 ≥ 12 GB（四条全用 ≥ 16 GB） | 精简版约 2~3 GB，完整版约 3~4 GB，表格产线约 2~3 GB；后台「服务控制 → 卸载」可释放。**Docker 还需 `MEM_LIMIT` 不低于这些值**（默认 8g） |
| 磁盘 | ≥ 20 GB（GPU 版 ≥ 30 GB） | 依赖（装好约 1.5~2 GB）+ 运行时 0.2 GB + 模型 2.3 GB + 运行期 data；GPU 离线包另占约 9 GB（构建/分发用，装好后约 7 GB） |
| GPU 显存 | ≥ 8 GB（建议 12 GB+） | `PP-StructureV3` 单页峰值约 2~4 GB；`OCR` 约 1~2 GB；多实例按倍数累加 |
| CPU | 4 核起，8 核更稳 | 推理是纯 CPU 计算，线程数由 Paddle 自管 |
| 端口 | 8000（可改 `server.port`） | 内网访问，建议防火墙限制来源网段 |
| 时区 | 容器已设 `Asia/Shanghai` | 非容器部署请确保系统时区正确（日志时间） |

### 12.4 常见"传上去跑不起来"的原因

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `install_offline.sh` 报 wheel 架构不匹配 | 拷了另一架构的 `offline/wheels/<arch>` | 在对应架构机器重新 `download_wheels.sh --arch <arch>` 并重新打包 |
| 报 `Python 版本与 wheel 不匹配` | 下载时 `--python` 与运行时版本不一致 | 重新下载 `--python 3.12`（与包内运行时一致） |
| 报 `解释器无法运行 ... GLIBC_x.xx not found` | 运行时与目标系统 glibc 不兼容 | 换更老的运行时（需 glibc ≤ 2.17 的构建），或用 `--system-python` |
| pip 报 `No matching distribution found for numpy/pillow/PyMuPDF` | 目标机 glibc < 2.28，而 wheel 是 `manylinux_2_28` 标签 | 换 Ubuntu 20.04+/Debian 10+/RHEL 8+；`preflight.sh` 第 1 步会提前报出 |
| GPU 包安装报 `缺少 paddlepaddle-gpu wheel` | 打包时 `--wheels-dir` 指到了 CPU 目录，或直接拷源码目录但没带 `--wheels-dir` | 重新打包：`make_offline_bundle.sh --arch amd64 --device gpu --wheels-dir offline/wheels-gpu/amd64` |
| GPU 包安装报「同时存在 CPU 版与 GPU 版 Paddle 轮子」 | 同一个 wheel 目录混放了两版 Paddle | 重新 `download_wheels.sh --device gpu --out offline/wheels-gpu`（会清掉 CPU 残留） |
| 启动报 `UnicodeDecodeError: 'utf-8' codec can't decode byte 0xa3` | `config/pipelines/` 里混入了非 UTF-8 文件：macOS 的 `._*.yaml`（AppleDouble 二进制）或被存成 GBK 的 yaml | 现版本已容错（跳过并记日志，不再崩溃）；顺手 `./scripts/preflight.sh` 第 4 步会列出可疑文件，删掉 `._*` 或另存为 UTF-8 即可 |
| 任务第 1 页就失败：`UnboundLocalError: ... 'page_mem'` | 1.0.0 老包 `manager._run()` 的 `page_mem` 只在 CPU 分支赋值，**GPU 机器上每页都失败**（CPU/MOCK 测不到） | 用 `dist/hotfix-1.0.0-patches.tar.gz` 覆盖 `backend/` 后重启；新整包已修复 |
| 报 `缺少随包运行时` / `系统也没有 python3` | 打包时漏了 `offline/python/<arch>` | 联网机执行 `./scripts/download_python_runtime.sh --arch <arch>` 后重新打包 |
| 启动报 `No module named fastapi` | 没跑 `install_offline.sh`，或跑了但用系统 python 启动 | 用 `scripts/start.sh`（会自动用 `.venv`） |
| `status.sh` 一直 503 | 模型目录为空 / 产线加载失败 | 看 `logs/app.log`；后台「模型清单」应为 17 个 |
| 识别很慢或超时 | 密集扫描件 120 s/页很常见 | 调大 `limits.task_timeout_sec`，或降 `pdf_dpi`，或换 `OCR` 产线 |
| 有 GPU 但仍走 CPU | 装的是 CPU 版 paddle / 容器没加 `--gpus` / 没重载 | 见 §10 Q11.5 |
| GPU 任务报 CUDA OOM | batch 过大或多实例抢显存 | 调小 `ocr.text_rec_batch_size`、`concurrency=1`、`gpu_mem_fraction` |

---

## 13. 自测与验证（开发者/交付验收）

| 测试 | 命令 | 覆盖内容 | 是否需要模型 |
| --- | --- | --- | --- |
| GPU 依赖闭包单测 | `python3 tests/test_gpu_deps.py` | Linux 标记依赖、无标记传递依赖、版本不符、`~=` 兼容范围、Windows/macOS/aarch64/emscripten/extras 不误报、`python_version` 按目标解释器求值（26 项） | 否 |
| GPU 安装路径回归（容器） | `bash tests/test_gpu_install_paths.sh` | GPU 轮子目录 `--device gpu` 能通过预检、混装 CPU/GPU 被拦、`--device cpu` 误配提示、`auto` 自动识别（4 场景 8 断言；需 docker，秒级） | 否 |
| 安装结果校验片段 | `python3 tests/test_install_verify_snippet.py` | 无驱动 + 容错=退出 0 且给 WARN；无驱动 + 严格=退出 1；依赖齐全=打印 `cuda=False`（8 项） | 否 |
| 配置编码容错 | `python3 tests/test_pipelines_encoding.py` | UTF-8 正常、GBK 配置正确解码、`._*.yaml` 二进制被跳过、截断 UTF-8 不崩、隐藏/空文件容忍（13 项） | 否 |
| GPU 路径回归 | `python3 tests/test_manager_gpu_path.py` | 用假 GPU 引擎把 `_run()` 真正跑一遍：GPU 分支完整跑完一页（旧代码此处 `UnboundLocalError: page_mem`）、日志带显存信息、`gpu_memory()` 抛异常时任务不受影响（8 项） | 否 |
| 设备解析单测 | `python3 tests/test_devices.py` | 设备串解析、auto 选择、Apple Silicon 强制 CPU、GPU 不可用报错、显存比例（29 项） | 否 |
| 内存保护单测 | `python3 tests/test_memguard.py` | cgroup v1/v2 限额解析、无限哨兵值、像素估算、余量不足时抛 `MemError`（18 项） | 否 |
| 表格解析单测 | `python3 tests/test_tableparse.py` | rowspan/colspan、缺失闭合标签、实体、GFM 表格（14 项） | 否 |
| 结果归一化单测 | `python3 tests/test_engine_utils.py` | PaddleX 结果外层包装解包、OCR 段落聚类、版面块映射、**表格产线 `pred_html`→网格（含 rowspan/colspan、无表格退回 OCR）**（21 项） | 否 |
| 端到端接口测试 | `bash tests/e2e.sh` | 上传（图片+多页 PDF）→ 队列/进度 → 结果结构 → 四种导出 → 后台登录/概览/日志 → 在线改配置 → **设备/档位/批处理切换** → 限额 413 → 暂停/恢复 → 删除 → **会话隔离** → 异常文件拒绝 | 否（MOCK 引擎） |
| 离线自检（默认产线） | `.venv/bin/python scripts/smoke_test.py` | 产线加载 + 真实推理 + 四格式导出 + docx/xlsx 回读 | **是** |
| 离线自检（指定/全部产线） | `... smoke_test.py --pipeline table_recognition_v2` / `--all` | 逐条产线跑同一套加载/推理/导出/回读；`--all` 会在产线间自动卸载释放内存 | **是** |
| 仅测 Web 链路 | `.venv/bin/python scripts/smoke_test.py --mock --all` | 同上但不加载模型（秒级） | 否 |

`tests/e2e.sh` 会在 8123 端口拉起一个 MOCK 服务（使用临时数据目录，不污染 `data/`），
执行完自动关闭。最近一次执行结果：**60 项全部通过**（含多产线 6 项、会话隔离 10 项、设备 4 项、离线文档 7 项）
（新浏览器列表为空、跨会话取结果/导出/取消/删除一律 404、篡改 Cookie 无数据泄露、
管理员可见全部并带会话标识）。

前端为原生 HTML/CSS/JS，无构建步骤、无 CDN；`/assets/*` 全部本地文件，
断网环境打开页面不会出现样式或脚本加载失败。

### 真实产线验收记录（PP-OCRv6 · 纯文字识别）

| 项目 | 结果 |
| --- | --- |
| 环境 | macOS arm64 / Python 3.12 / `paddlex 3.7.2` / CPU（Apple Silicon 无 GPU 后端） |
| 模型 | `PP-OCRv6_medium_det` + `PP-OCRv6_medium_rec`（约 133 MB，首次加载 2.2 s） |
| 输入 | A4 报价单 PNG（1654×2339） |
| 输出 | 38 个文本块 / 300 字符；`rec_texts` 41 条（按阅读顺序聚类） |
| 耗时 | **14.6 s**（CPU，含检测+识别；同机 PP-StructureV3 约 48 s） |
| 导出 | txt/md 正常，字段与 v5 一致（无需改解析代码） |
| 档位切换 | 后台改 `ocr_tier` + 重载后日志显示 `PP-OCRv6_medium/small/tiny` 与新的 batch 值 |

### 真实产线验收记录（PP-StructureV3）

| 项目 | 结果 |
| --- | --- |
| 环境 | macOS arm64 / Python 3.12 / `paddlepaddle 3.3.1` / `paddlex 3.7.2` / CPU |
| 模型 | 17 个，约 2.36 GB（四条产线共用大部分权重；含 PP-OCRv5 与 PP-OCRv6 各一套 OCR 权重） |
| 产线加载 | 7~9 秒 |
| 输入 | A4 报价单 PDF（含 5 处合并单元格的 6×7 表格）、2 页 PDF、PNG 截图 |
| 表格还原 | 6×7；合并位置 `(0,0,2,1) (0,1,1,3) (0,4,2,1) (0,5,1,2) (5,0,1,4)` 与样张一致 |
| Excel 映射 | 合并区域 `A1:A2`、`B1:D1`、`E1:E2`、`F1:G1`、`A6:D6`；`39900 / 2150 / 1250 / 45450` 为数字型，文本列不被截断 |
| Word 导出 | 真实表格 + 单元格合并且可回读 |
| 阅读顺序 | 抬头 → 标题 → 表格 → 备注，页眉/页脚被识别为 `header/footer`（默认不导出） |
| 单页耗时 | 30~60 秒（简单版面）；密集公文扫描件实测 120 秒 |

### 真实产线验收记录（通用表格识别 table_recognition_v2）

| 项目 | 结果 |
| --- | --- |
| 环境 | 同上（macOS arm64 / Python 3.12 / `paddlex 3.7.2` / CPU） |
| 模型 | 与精简版共用，仅新增 `SLANeXt_wireless`；全库 **17 个 / 2.3 GB** |
| 产线加载 | 首次 ~11 s（含表格方向分类/结构识别/单元格检测） |
| 输入 | 同一份 A4 报价单 PDF（6×7 表格，5 处合并单元格） |
| 表格还原 | 6×7；合并位置 `(0,0,2,1) (0,1,1,3) (0,4,2,1) (0,5,1,2) (5,0,1,4)`，与样张**完全一致** |
| 表格位置 | 由 `cell_box_list` 并集得到 `[120, 380, 1550, 836]`，与版面位置吻合 |
| Excel 映射 | 合并区域 `A1:A2`、`B1:D1`、`E1:E2`、`F1:G1`、`A6:D6`；`39900 / 2150 / 1250 / 45450` 为数字型 |
| 单页耗时 | 约 80~90 s（首次含模型加载）；纯表格页比精简版面解析略慢，但表格更稳 |

**已知限制**：跨度特别大的合并行（例如 5 列并为 1 格）有概率少并 1 列，
内容不丢失，导出 Excel 后手动合并一格即可；如需完全精确，可调大 `pdf_dpi` 后重识别。

---

## 14. 许可与致谢

本项目调用 [PaddleX](https://github.com/PaddlePaddle/PaddleX) 官方产线与模型，
使用前请遵守 PaddlePaddle / PaddleX 及其模型的开源许可（Apache-2.0）。

---

