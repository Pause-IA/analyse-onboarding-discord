FROM python:3.12-slim
WORKDIR /app
COPY export.py .
ENV DATA_DIR=/data PYTHONUNBUFFERED=1
CMD ["python3", "export.py", "loop"]
