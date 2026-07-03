FROM python:3.11-slim

# Installiere Chromium und den Webdriver
RUN apt-get update && apt-get install -y \
    chromium \
    chromium-driver \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Docker merkt automatisch, wenn sich diese Datei auf GitHub ändert
# und updatet sie bei einem "Pull and redeploy"!
COPY app.py .

EXPOSE 5000

CMD ["python", "app.py"]
