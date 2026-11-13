#!/usr/bin/env bash
# 容器启动入口：准备数据目录与默认配置，然后 exec 到 Python 进程。
#
# 为什么需要它：
# * 项目用 **cwd** 决定运行期目录（<cwd>/.aieb），容器里 cwd 固定为 AIEB_DATA_DIR；
# * 首次启动时 .aieb/config.yaml 不存在会直接退出，这里用镜像内的模板兜底；
# * 绑定 0.0.0.0（容器内必须这样）时，项目安全模型要求必须提供 token，
#   这里提前给出可操作的中文报错，而不是让用户看 RemoteAuthError 堆栈。
set -euo pipefail

DATA_ROOT="${AIEB_DATA_DIR:-/data}"
mkdir -p "${DATA_ROOT}/.aieb"

CONFIG="${DATA_ROOT}/.aieb/config.yaml"
if [ ! -f "${CONFIG}" ]; then
    cp /app/docker/config.docker.yaml "${CONFIG}"
    echo "[entrypoint] 未找到 ${CONFIG}，已按模板生成（不含任何密钥，密钥请在运行时注入）"
fi

HOST="${AIEB_HOST:-0.0.0.0}"
PORT="${AIEB_PORT:-18888}"

EXTRA_ARGS=()
if [ -z "${AIEB_REMOTE_TOKEN:-}" ]; then
    if [ "${AIEB_ALLOW_UNAUTHENTICATED_REMOTE:-0}" = "1" ]; then
        EXTRA_ARGS+=(--allow-unauthenticated-remote)
        echo "[entrypoint] 警告：未设置 AIEB_REMOTE_TOKEN，且显式允许无认证暴露（仅限受控网络）" >&2
    else
        cat >&2 <<'EOF'
[entrypoint] 启动中止：绑定 0.0.0.0 必须提供访问 token。

项目默认只监听 127.0.0.1；容器内必须监听 0.0.0.0，因此沿用原安全模型要求 token：
  1) 在项目根 .env 里设置 AIEB_REMOTE_TOKEN=<一串随机字符串>，然后 docker compose up -d
  2) 浏览器访问 http://localhost:18888/#token=<同一串>
如果你确实在完全受控的内网演示，可设置 AIEB_ALLOW_UNAUTHENTICATED_REMOTE=1（不推荐）。
EOF
        exit 64
    fi
fi

cd "${DATA_ROOT}"
echo "[entrypoint] 启动 AI 外脑 Demo：host=${HOST} port=${PORT} data=${DATA_ROOT}"
exec python -m aieb.brain.cli --host "${HOST}" --port "${PORT}" "${EXTRA_ARGS[@]}"
