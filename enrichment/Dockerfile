FROM python:3.12-slim
WORKDIR /app
COPY enrichment/ /app/enrichment/
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
CMD ["python", "enrichment/service.py"]
