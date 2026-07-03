FROM python:3.11-slim

# Installiere Chromium und den Webdriver
RUN apt-get update && apt-get install -y \
    chromium \
    chromium-driver \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Wir kopieren NUR die requirements.txt, damit die Pakete installiert werden.
# Die app.py wird später automatisch über die docker-compose live reingeladen!
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

EXPOSE 5000

CMD ["python", "app.py"]
