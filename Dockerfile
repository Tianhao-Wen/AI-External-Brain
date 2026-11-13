# AI 外脑后端镜像（Python + FastEmbed/ONNX + LangGraph + Qdrant Client）
#
# 设计要点：
# 1. 依赖层与源码层分离：先 COPY requirements-docker.txt 装依赖，再 COPY 源码，
#    改业务代码时不会让 pip 层失效。
# 2. 代码装在 /app 并安装进 site-packages；**运行目录是 /data**（可挂 Volume），
#    因为项目用 cwd 决定运行期目录（<cwd>/.aieb），这样代码与数据彻底分开。
# 3. 非 root 运行（uid 10001），/data 归该用户所有，命名 Volume 首次创建时会继承属主。
# 4. 不带任何 .env / config.yaml / SQLite / PDF：镜像里只有代码与配置模板。
# 5. tini 作为 PID 1，保证 Ctrl+C / docker stop 能正确传递信号并回收子进程。
#
# 与本地 Windows 开发的兼容性：不修改任何本地启动方式，单靠镜像层完成部署。

ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim

# ---- 基础环境 -------------------------------------------------------------
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    PYTHONPATH=/app \
    AIEB_DATA_DIR=/data \
    AIEB_PORT=18888 \
    AIEB_EMBEDDING_CACHE_DIR=/data/.cache/fastembed \
    HF_HOME=/data/.cache/huggingface \
    TZ=Asia/Shanghai

# onnxruntime 需要 libgomp1；curl 便于容器内排障；tini 作为 init 进程
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libgomp1 \
        ca-certificates \
        curl \
        tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ---- 依赖层（缓存友好）----------------------------------------------------
COPY requirements.txt requirements-docker.txt ./
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements-docker.txt

# ---- 应用层 ---------------------------------------------------------------
COPY pyproject.toml README.md ./
COPY aieb ./aieb
RUN python -m pip install --no-deps . \
    && python -c "import aieb, aieb.brain, aieb.remote; print('aieb import ok')"

# ---- 容器启动脚本与配置模板（不含任何密钥）--------------------------------
COPY docker /app/docker
# 容器内自检脚本（上传 PDF / Hybrid 检索 / 工作流冒烟）
COPY scripts/docker_smoke_test.py /app/scripts/docker_smoke_test.py
RUN chmod +x /app/docker/entrypoint.sh

# ---- 运行用户与数据目录 ---------------------------------------------------
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin aieb \
    && mkdir -p /data/.aieb \
    && chown -R aieb:aieb /data

USER aieb
WORKDIR /data

EXPOSE 18888

# 存活探针：进程活着就 200；就绪探针 /readyz 会额外检查 Qdrant（compose 里覆盖为 /readyz）
HEALTHCHECK --interval=20s --timeout=5s --start-period=40s --retries=5 \
    CMD python -c "import os,sys,urllib.request; p=os.environ.get('AIEB_PORT','18888'); sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/healthz' % p, timeout=4).status==200 else 1)"

ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker/entrypoint.sh"]
