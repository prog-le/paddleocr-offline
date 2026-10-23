# =============================================================================
# PaddleX OCR Web 镜像（单 Dockerfile 支持 linux/amd64 与 linux/arm64）
#
# 关键点：
#   * 用 TARGETARCH 选择对应架构的离线 wheel 目录（offline/wheels/amd64|arm64）；
#   * 镜像内 pip 安装使用 --no-index，完全不访问 PyPI；
#   * 模型全部 COPY 进镜像，容器启动不下载任何东西；
#   * 基础镜像与 apt 依赖只在【构建机】需要网络，离线服务器只做 docker load；
#   * Python 版本固定 3.12，与本地部署的随包运行时（CPython 3.12）保持一致，
#     两套 wheel（offline/wheels/<arch>）可以共用同一份。
#
# 构建（在项目根目录执行）：
#   docker buildx build --platform linux/amd64 -f docker/Dockerfile \
#     --build-arg TARGETARCH=amd64 -t paddlex-ocr-web:1.0.0-amd64 --load .
#   或直接用脚本：docker/build_image.sh --arch amd64
# =============================================================================
ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim-bookworm

ARG TARGETARCH=amd64
ARG APP_VERSION=1.0.0
ARG DEBIAN_FRONTEND=noninteractive

LABEL org.opencontainers.image.title="PaddleX OCR Web" \
      org.opencontainers.image.description="Offline OCR + table recognition (PaddleX PP-StructureV3, CPU)" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.arch="${TARGETARCH}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai \
    LANG=C.UTF-8 \
    PADDLE_PDX_CACHE_HOME=/app/models/paddlex_home \
    PADDLE_PDX_MODEL_SOURCE=BOS \
    PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True \
    PADDLE_PDX_DISABLE_TELEMETRY=True \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    MODELSCOPE_OFFLINE=1 \
    PADDLEX_OCR_DATA_DIR=/app/data \
    PADDLEX_OCR_LOG_DIR=/app/logs \
    PADDLEX_OCR_CONFIG=/app/config/config.yaml

# 运行期系统依赖：Paddle 需要 libgomp；opencv-contrib-python 需要 libGL / libglib
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 \
        libgl1 \
        libglib2.0-0 \
        libsm6 \
        libxext6 \
        libxrender1 \
        tzdata \
        ca-certificates \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ---- Python 依赖：完全离线安装（不访问 PyPI）----
COPY requirements.txt /app/requirements.txt
COPY offline/wheels/${TARGETARCH}/ /tmp/wheels/
RUN python -m pip install --no-index --find-links=/tmp/wheels -r /app/requirements.txt \
    && python -c "import paddle, paddlex; print('paddle', paddle.__version__, '/ paddlex', paddlex.__version__)" \
    && rm -rf /tmp/wheels

# ---- 应用代码与前端静态资源 ----
COPY backend/ /app/backend/
COPY frontend/ /app/frontend/
COPY config/ /app/config/
COPY scripts/ /app/scripts/

# ---- 模型（离线内置，启动即用）----
COPY models/ /app/models/

# ---- 运行用户与目录权限 ----
RUN useradd -m -u 10001 -s /bin/bash ocr \
    && mkdir -p /app/data /app/logs \
    && chown -R ocr:ocr /app/data /app/logs /app/models \
    && chmod +x /app/scripts/*.sh 2>/dev/null || true

COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

USER ocr
WORKDIR /app

EXPOSE 8000

# 200=健康；503=服务在线但模型/产线降级（仍视为存活，便于登录后台排查）
# 推理时单进程会短时占满 CPU，健康检查偶尔变慢；超时给足、重试放宽，避免误判 unhealthy
HEALTHCHECK --interval=30s --timeout=15s --start-period=90s --retries=5 \
    CMD python /app/scripts/docker_health.py

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["python", "-m", "uvicorn", "backend.main:app", \
     "--host", "0.0.0.0", "--port", "8000", "--workers", "1", \
     "--no-access-log", "--timeout-keep-alive", "75"]
