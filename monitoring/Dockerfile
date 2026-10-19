FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY exporter.py /app/exporter.py
USER 65534:65534
CMD ["python", "/app/exporter.py", "--artifacts", "/artifacts", "--config", "/config/sources.json", "--host", "0.0.0.0"]
