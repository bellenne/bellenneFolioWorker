FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY folio_worker ./folio_worker
ENV PYTHONUNBUFFERED=1 DATA_DIR=/data SPOOL_DIR=/spool
VOLUME /data /spool
EXPOSE 8000
CMD ["uvicorn", "folio_worker.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
