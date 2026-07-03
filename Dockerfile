FROM python:3.11-slim

# Installiere Chromium und den passenden Webdriver für den Headless-Betrieb
RUN apt-get update && apt-get install -y \
    chromium \
    chromium-driver \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

EXPOSE 5000

# Starte den Flask Web-Server
CMD ["python", "app.py"]
