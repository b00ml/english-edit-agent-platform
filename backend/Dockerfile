# backend/Dockerfile —— 后端服务镜像
# 基础镜像：Python 3.12 精简版
FROM python:3.15-slim

# 设置工作目录
WORKDIR /code

# 设置环境变量（避免生成 .pyc、启用无缓冲输出）
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# 先复制依赖清单以利用镜像层缓存（psycopg2-binary 自带 wheel，无需编译工具）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY . .

# 暴露服务端口
EXPOSE 8000

# 启动命令：
# 生产环境应先在入口执行 Alembic 迁移（alembic upgrade head）再启动服务，
# 示例：
#   CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]