FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 依赖单独一层，改代码不必重装
COPY requirements.txt ./
RUN pip install -r requirements.txt

# 只放运行需要的代码；config / data / .env 由 compose 挂载
COPY bot/ ./bot/
COPY tools/ ./tools/
COPY main.py ./

# 默认只是占位，真正的命令写在 docker-compose.yml 里
CMD ["python", "main.py", "paper", "--market", "perp"]
