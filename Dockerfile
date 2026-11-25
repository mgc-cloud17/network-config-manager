# Imagen endurecida de Network Config Manager
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DB_PATH=/app/data/network_manager.db

WORKDIR /app

# Dependencias fijadas (incluye paramiko==3.5.1 por compatibilidad con Cisco IOSv)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Usuario sin privilegios y carpeta de datos escribible para SQLite
RUN useradd --create-home appuser \
    && mkdir -p /app/data

COPY app.py cloud_storage.py crypto.py database.py net_automation.py ./
COPY templates ./templates

RUN chown -R appuser:appuser /app

USER appuser

EXPOSE 5000

# ENCRYPTION_KEY (y opcionalmente ENABLE_S3_BACKUP / AWS_S3_BUCKET / AWS_REGION)
# se inyectan en tiempo de ejecución; nunca se hornean en la imagen.
CMD ["gunicorn", "--preload", "-w", "2", "-b", "0.0.0.0:5000", "app:app"]
