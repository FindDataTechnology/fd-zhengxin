# fd-zhengxin-api 服务端镜像 — FastAPI + uvicorn。
# 发布通道：GitHub Actions -> TCR（.github/workflows/image.yml）。形制照 fd-zhesi。
# 硬性要求：上游源为默认；国内镜像源一律 ARG 且默认空。
# 与 fd-zhesi 差异：参照系不随镜像安装（分库先例，服务地址见 config.py）。
FROM python:3.12-slim

ARG PIP_INDEX_URL=""

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY fd_zhengxin_api ./fd_zhengxin_api

RUN if [ -n "$PIP_INDEX_URL" ]; then pip install --no-cache-dir -i "$PIP_INDEX_URL" .; else pip install --no-cache-dir .; fi \
 && useradd -m -u 1000 appuser

USER appuser

EXPOSE 8000

CMD ["uvicorn", "fd_zhengxin_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
