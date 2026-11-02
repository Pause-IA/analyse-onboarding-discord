# Lancement ponctuel isolé (voir README) : le conteneur fait un export puis s'arrête.
FROM python:3.12-slim
WORKDIR /app
COPY export.py .
ENV CACHE_DIR=/cache OUT_DIR=/out PYTHONUNBUFFERED=1
ENTRYPOINT ["python3", "export.py"]
CMD ["run"]
