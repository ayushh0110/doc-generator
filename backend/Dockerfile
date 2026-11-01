FROM python:3.12-slim

# LibreOffice renders the previews and recalculates workbooks.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libreoffice-writer libreoffice-impress libreoffice-calc fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ .

RUN useradd --create-home redline && mkdir -p /data/storage && chown -R redline /data /app
USER redline

ENV STORAGE_DIR=/data/storage
EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
