FROM python:3.12-slim

# 从 uv 官方镜像拷二进制，比 pip install uv 快；0.12 和本机生成 uv.lock 的版本一致
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# 先装依赖再拷代码：只改代码时这一层命中缓存，重新 build 几秒钟
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY c5bot ./c5bot

# 配置和数据都不进镜像，运行时挂进来：.env（env_file）、watchlist.toml、data/
EXPOSE 8766
CMD ["python", "-m", "c5bot", "run"]
