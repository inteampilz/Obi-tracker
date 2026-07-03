import os
import time
import threading
import requests
import datetime
from flask import Flask, request, render_template_string, redirect, url_for
from selenium import webdriver
from selenium.webdriver.chrome.options import Options

app = Flask(__name__)

# Konfiguration (startet mit Environment-Variablen, überschreibbar in der Web-UI)
CONFIG = {
    "url": "https://www.obi.ch/klimageraete/midea-mobile-split-klimaanlage-portasplit/p/6088348",
    "interval": 15,
    "pushover_user": os.getenv("PUSHOVER_USER_KEY", ""),
    "pushover_token": os.getenv("PUSHOVER_APP_TOKEN", "")
}

# Status-Speicher für die Web-UI
STATE = {
    "is_running": False,
    "last_check": "Noch nie",
    "status": "Gestoppt"
}

tracker_thread = None
stop_event = threading.Event()

def send_pushover(message):
    try:
        requests.post("https://api.pushover.net/1/messages.json", data={
            "token": CONFIG["pushover_token"],
            "user": CONFIG["pushover_user"],
            "message": message,
            "title": "OBI Tracker",
            "url": CONFIG["url"],
            "url_title": "Zum OBI Shop"
        })
    except Exception as e:
        print(f"Pushover Fehler: {e}")

def check_availability():
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36")
    
    # Treiber starten
    driver = webdriver.Chrome(options=options)
    try:
        driver.get(CONFIG["url"])
        time.sleep(5) # Warten bis JS geladen ist
        page_text = driver.page_source.lower()
        
        if "online ausverkauft" in page_text or "derzeit nicht verfügbar" in page_text:
            return False
            
        if "in den warenkorb" in page_text or "lieferung möglich" in page_text or "abholung" in page_text:
            return True
            
        return False
    finally:
        # Sehr wichtig: Chrome nach jedem Check schließen, sonst läuft der RAM voll!
        driver.quit()

def tracker_loop():
    while not stop_event.is_set():
        STATE["last_check"] = datetime.datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        STATE["status"] = "Prüfe Webseite..."
        
        try:
            available = check_availability()
            if available:
                STATE["status"] = "✅ VERFÜGBAR! Nachricht gesendet."
                send_pushover("🚨 ALARM! Die Midea PortaSplit ist bei OBI wieder verfügbar!")
                # Wenn gefunden, 24 Stunden (86400 Sekunden) warten, um Spam zu verhindern
                stop_event.wait(86400)
            else:
                STATE["status"] = "❌ Nicht verfügbar."
        except Exception as e:
            STATE["status"] = f"⚠️ Fehler: {str(e)}"
        
        # Warten bis zum nächsten Check
        stop_event.wait(CONFIG["interval"] * 60)

# --- WEB UI (HTML Template) ---
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="de">
<head>
    <meta charset="UTF-8">
    <title>OBI Tracker Dashboard</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
</head>
<body class="bg-light">
<div class="container mt-5 max-w-md" style="max-width: 800px;">
    <div class="card shadow">
        <div class="card-header bg-primary text-white d-flex justify-content-between align-items-center">
            <h4 class="mb-0">🤖 OBI Verfügbarkeits-Tracker</h4>
            <span class="badge {{ 'bg-success' if state.is_running else 'bg-danger' }}">
                {{ 'AKTIV' if state.is_running else 'GESTOPPT' }}
            </span>
        </div>
        <div class="card-body">
            
            <div class="alert alert-info">
                <strong>Letzter Check:</strong> {{ state.last_check }} <br>
                <strong>Status:</strong> {{ state.status }}
            </div>

            <form action="/save" method="POST">
                <div class="mb-3">
                    <label class="form-label">OBI Produkt-URL</label>
                    <input type="url" name="url" class="form-control" value="{{ config.url }}" required>
                </div>
                <div class="row mb-3">
                    <div class="col">
                        <label class="form-label">Pushover User Key</label>
                        <input type="text" name="pushover_user" class="form-control" value="{{ config.pushover_user }}" required>
                    </div>
                    <div class="col">
                        <label class="form-label">Pushover App Token</label>
                        <input type="text" name="pushover_token" class="form-control" value="{{ config.pushover_token }}" required>
                    </div>
                </div>
                <div class="mb-3">
                    <label class="form-label">Prüf-Intervall (in Minuten)</label>
                    <input type="number" name="interval" class="form-control" value="{{ config.interval }}" min="1" required>
                </div>
                <button type="submit" class="btn btn-secondary w-100 mb-3">Einstellungen Speichern</button>
            </form>
            
            <hr>
            <div class="d-flex gap-2">
                {% if not state.is_running %}
                    <a href="/start" class="btn btn-success w-50">Tracker Starten</a>
                {% else %}
                    <a href="/stop" class="btn btn-danger w-50">Tracker Stoppen</a>
                {% endif %}
                <a href="/test" class="btn btn-outline-info w-50">Test-Nachricht senden</a>
            </div>
        </div>
    </div>
</div>
</body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE, config=CONFIG, state=STATE)

@app.route("/save", methods=["POST"])
def save():
    CONFIG["url"] = request.form["url"]
    CONFIG["pushover_user"] = request.form["pushover_user"]
    CONFIG["pushover_token"] = request.form["pushover_token"]
    CONFIG["interval"] = int(request.form["interval"])
    return redirect(url_for("index"))

@app.route("/start")
def start():
    global tracker_thread
    if not STATE["is_running"]:
        stop_event.clear()
        tracker_thread = threading.Thread(target=tracker_loop, daemon=True)
        tracker_thread.start()
        STATE["is_running"] = True
        STATE["status"] = "Gestartet..."
    return redirect(url_for("index"))

@app.route("/stop")
def stop():
    if STATE["is_running"]:
        stop_event.set()
        STATE["is_running"] = False
        STATE["status"] = "Gestoppt"
    return redirect(url_for("index"))

@app.route("/test")
def test_push():
    send_pushover("Dies ist eine Test-Nachricht aus dem Docker-Web-Interface!")
    return redirect(url_for("index"))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
