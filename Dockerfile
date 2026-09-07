# ===== 基础镜像（Python 3.11 精简版，体积小、启动快）=====
FROM python:3.11-slim

# 工作目录
WORKDIR /app

# 环境变量：
#   PYTHONDONTWRITEBYTECODE=1  禁止生成 .pyc 字节码（减小镜像体积）
#   PYTHONUNBUFFERED=1         关闭 stdout 缓冲，日志实时输出
#   HF_HOME                    本地 Embedding 模型的 HuggingFace 缓存目录
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/root/.cache/huggingface

# 系统依赖（build-essential 编译扩展；libgl1/libglib2.0-0 供 PDF 渲染/OCR 使用）
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# 依赖安装（先单独 COPY requirements.txt，利用 Docker 层缓存：依赖不变则跳过重装）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 可选：本地 Embedding 模型支持（取消注释以启用）
# RUN pip install --no-cache-dir sentence-transformers

# 复制项目源码（.dockerignore 已排除 .env/数据/日志等）
COPY . .

# 服务监听端口
EXPOSE 8008

# 健康检查：通过 /api/health 探活，失败即判定容器异常
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8008/api/health')" || exit 1

# 启动（单 worker，向量库/内存态共享，避免多进程状态不一致）
CMD ["uvicorn", "serve.main:app", "--host", "0.0.0.0", "--port", "8008", "--workers", "1"]
