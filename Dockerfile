# 薄部署层：把问答 API 和知识库命令行装进一个镜像。
# 注意：requirements.txt 含 sentence-transformers（会带上 torch），首次构建要下载数 GB；
# embedding 模型（bge-large-zh）首次启用时再下载约 1.3GB，缓存在 hf_cache 卷里。
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
EXPOSE 8000
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]
