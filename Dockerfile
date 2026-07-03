FROM python:3.11-slim

# Installiere Chromium und den Webdriver
RUN apt-get update && apt-get install -y \
    chromium \
    chromium-driver \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# NEU: Diese Zeile zwingt Portainer dazu, den Cache ab hier wegzuwerfen
ENV FORCE_REBUILD=2023_11_NEW_DASHBOARD

COPY app.py .

EXPOSE 5000

# Starte den Flask Web-Server
CMD ["python", "app.py"]
