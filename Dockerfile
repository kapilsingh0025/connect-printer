FROM python:3.12-slim

# LibreOffice: Word, Excel, PowerPoint, Text files ko PDF banane ke liye
RUN apt-get update && apt-get install -y --no-install-recommends \
      libreoffice-writer libreoffice-calc libreoffice-impress \
      fonts-liberation fonts-dejavu-core fonts-noto-core \
      fonts-crosextra-carlito fonts-crosextra-caladea \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py .

CMD gunicorn server:app --workers 1 --threads 4 --timeout 180 --bind 0.0.0.0:${PORT:-10000}
