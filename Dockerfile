FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FIRMWARE_DB_PATH=/data/firmware_release.db

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# SQLite 数据文件所在目录，由 compose 挂载命名卷持久化
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8080

# 单 worker：SQLite 的写事务在单进程内串行，配合 BEGIN IMMEDIATE
# 即可保证并发回执等价于某个串行顺序；多 worker 反而会增加锁竞争。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
