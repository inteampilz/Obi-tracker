FROM python:3.11-slim

# Installiere Chromium und den Webdriver
RUN apt-get update && apt-get install -y \
    chromium \
    chromium-driver \
    && rm -rf /var/lib/apt/lists/*

# NEU: Ein anderer Ordnername, damit der Docker-Cache sofort platzt
WORKDIR /app_v3

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Ein Zeitstempel als ultimativer Cache-Brecher
ENV BUILD_DATE="2026-07-03"

COPY app.py .

EXPOSE 5000

CMD ["python", "app.py"]
