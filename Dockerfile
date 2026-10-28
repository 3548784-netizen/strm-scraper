# 基于官方便携版 Python，ffmpeg 从系统包安装，镜像约 300 MB
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tzdata ca-certificates \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY scraper ./scraper
COPY static ./static
COPY config.example.yaml ./
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# /media 放媒体目录（只读挂载也能用，只要产物目录可写）
# /config 存配置和状态库，必须持久化，否则重启会丢失进度
VOLUME ["/config"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/api/health',timeout=3)"

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "scraper.main"]
