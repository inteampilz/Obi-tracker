import os
import json
import time
import uuid
import threading
import requests
import datetime
from flask import Flask, request, render_template_string, redirect, url_for, send_file
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

app = Flask(__name__)

DATA_DIR = "data"
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
config_lock = threading.Lock()

# Standard-Konfiguration
CONFIG = {
    "pushover_user": os.getenv("PUSHOVER_USER_KEY", ""),
    "pushover_token": os.getenv("PUSHOVER_APP_TOKEN", ""),
    "interval": 15,
    "items": [] # Hier landen die verschiedenen Artikel
}

STATE = {
    "is_running": False,
    "status": "Gestoppt"
}

tracker_thread = None
stop_event = threading.Event()

# --- DATEN-VERWALTUNG ---
def init_data_dir():
    if not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR)

def load_config():
    global CONFIG
    init_data_dir()
    if os.path.exists(CONFIG_FILE):
        with config_lock:
            try:
                with open(CONFIG_FILE, "r") as f:
                    saved_conf = json.load(f)
                    CONFIG.update(saved_conf)
            except Exception as e:
                print(f"Fehler beim Laden der Config: {e}")
    else:
        save_config()

def save_config():
    init_data_dir()
    with config_lock:
        with open(CONFIG_FILE, "w") as f:
            json.dump(CONFIG, f, indent=4)

# --- PUSHOVER ---
def send_pushover(message, item_url, image_path=None):
    try:
        data = {
            "token": CONFIG["pushover_token"],
            "user": CONFIG["pushover_user"],
            "message": message,
            "title": "Produkt-Tracker Alarm",
            "url": item_url,
            "url_title": "Direkt zum Artikel"
        }
        files = {}
        if image_path and os.path.exists(image_path):
            files["attachment"] = ("screenshot.png", open(image_path, "rb"), "image/png")
        
        if files:
            requests.post("https://api.pushover.net/1/messages.json", data=data, files=files)
        else:
            requests.post("https://api.pushover.net/1/messages.json", data=data)
    except Exception as e:
        print(f"Pushover Fehler: {e}")

# --- TRACKER LOGIK ---
def setup_driver():
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36")
    return webdriver.Chrome(options=options)

def check_item(driver, item):
    try:
        driver.get(item["url"])
        time.sleep(5) # Warten bis JS geladen ist
        
        body_text = driver.find_element(By.TAG_NAME, "body").text.lower()
        
        body_text = body_text.replace("keine lieferung", "xxx").replace("keine abholung", "xxx")
        body_text = body_text.replace("nicht lieferbar", "xxx").replace("nicht verfügbar", "xxx")
        
        if "online ausverkauft" in body_text or "derzeit nicht verfügbar" in body_text or "ausverkauft" in body_text:
            return False
            
        if "in den warenkorb" in body_text or "lieferung möglich" in body_text or "marktabholung" in body_text:
            screenshot_path = os.path.join(DATA_DIR, f"screenshot_{item['id']}.png")
            driver.save_screenshot(screenshot_path)
            item["has_screenshot"] = True
            item["screenshot_time"] = time.time()
            return True
            
        return False
    except Exception as e:
        print(f"Fehler bei {item['name']}: {e}")
        return False

def tracker_loop():
    while not stop_event.is_set():
        STATE["status"] = "Prüfe Artikel..."
        
        # Prüfen ob überhaupt Artikel da sind, die nicht gerade im 24h-Cooldown sind
        needs_check = False
        for item in CONFIG["items"]:
            if time.time() - item.get("found_time", 0) > 86400:
                needs_check = True
                
        if needs_check and CONFIG["items"]:
            driver = setup_driver()
            try:
                for item in CONFIG["items"]:
                    if stop_event.is_set(): break
                    
                    # Überspringen, wenn in den letzten 24h schon gefunden
                    if time.time() - item.get("found_time", 0) <= 86400:
                        continue
                        
                    item["last_check"] = datetime.datetime.now().strftime("%d.%m.%Y %H:%M:%S")
                    
                    is_available = check_item(driver, item)
                    if is_available:
                        item["status"] = "✅ VERFÜGBAR!"
                        item["found_time"] = time.time() # Löst den 24h Cooldown aus
                        img_path = os.path.join(DATA_DIR, f"screenshot_{item['id']}.png")
                        send_pushover(f"🚨 ALARM! {item['name']} ist verfügbar!", item["url"], img_path)
                    else:
                        item["status"] = "❌ Nicht verfügbar."
                        
                    save_config() # Status nach jedem Artikel speichern
                    
            finally:
                driver.quit()
                
        STATE["status"] = f"Warte {CONFIG['interval']} Min..."
        stop_event.wait(CONFIG["interval"] * 60)

# --- WEB UI HTML ---
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="de">
<head>
    <meta charset="UTF-8">
    <title>Universal Tracker Dashboard</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
        .screenshot-thumb { max-height: 150px; cursor: pointer; transition: 0.3s; }
        .screenshot-thumb:hover { opacity: 0.8; }
    </style>
</head>
<body class="bg-light pb-5">
<div class="container mt-4" style="max-width: 900px;">
    
    <div class="d-flex justify-content-between align-items-center mb-4">
        <h2>🛒 Universal Produkt-Tracker</h2>
        <div>
            {% if not state.is_running %}
                <a href="/start" class="btn btn-success">▶ Tracker Starten</a>
            {% else %}
                <a href="/stop" class="btn btn-danger">⏸ Tracker Stoppen</a>
            {% endif %}
        </div>
    </div>

    <!-- Status Banner -->
    <div class="alert alert-{{ 'success' if state.is_running else 'secondary' }} d-flex justify-content-between">
        <span><strong>System-Status:</strong> {{ state.status }}</span>
        <span>Tracker ist <strong>{{ 'AKTIV' if state.is_running else 'GESTOPPT' }}</strong></span>
    </div>

    <!-- Tracking Liste -->
    <div class="card shadow-sm mb-4">
        <div class="card-header bg-dark text-white">
            <h5 class="mb-0">📦 Überwachte Artikel ({{ config.items|length }})</h5>
        </div>
        <div class="card-body p-0">
            <table class="table table-hover mb-0">
                <thead class="table-light">
                    <tr>
                        <th>Produkt</th>
                        <th>Status</th>
                        <th>Letzter Check</th>
                        <th>Beweis</th>
                        <th class="text-end">Aktion</th>
                    </tr>
                </thead>
                <tbody>
                    {% for item in config.items %}
                    <tr>
                        <td>
                            <strong>{{ item.name }}</strong><br>
                            <a href="{{ item.url }}" target="_blank" class="text-muted small">🔗 Zum Shop</a>
                        </td>
                        <td>
                            <span class="badge {{ 'bg-success' if 'VERFÜGBAR' in item.status else 'bg-secondary' }}">
                                {{ item.status }}
                            </span>
                            {% if item.found_time and (time.time() - item.found_time) < 86400 %}
                                <br><small class="text-warning">Auf 24h-Cooldown</small>
                            {% endif %}
                        </td>
                        <td class="small">{{ item.last_check }}</td>
                        <td>
                            {% if item.has_screenshot %}
                                <a href="/screenshot/{{ item.id }}?t={{ item.screenshot_time }}" target="_blank">
                                    <img src="/screenshot/{{ item.id }}?t={{ item.screenshot_time }}" class="screenshot-thumb img-thumbnail">
                                </a>
                            {% else %}
                                <span class="text-muted small">Kein Bild</span>
                            {% endif %}
                        </td>
                        <td class="text-end">
                            {% if item.found_time and (time.time() - item.found_time) < 86400 %}
                                <a href="/reset_cooldown/{{ item.id }}" class="btn btn-sm btn-outline-warning mb-1">Cooldown Reset</a><br>
                            {% endif %}
                            <a href="/delete/{{ item.id }}" class="btn btn-sm btn-outline-danger">Löschen</a>
                        </td>
                    </tr>
                    {% else %}
                    <tr>
                        <td colspan="5" class="text-center py-4 text-muted">Keine Artikel angelegt. Füge unten einen hinzu!</td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
    </div>

    <!-- Neuer Artikel Formular -->
    <div class="card shadow-sm mb-4 border-primary">
        <div class="card-header bg-primary text-white">➕ Neuen Artikel hinzufügen</div>
        <div class="card-body">
            <form action="/add" method="POST" class="row g-2">
                <div class="col-md-4">
                    <input type="text" name="name" class="form-control" placeholder="Produktname (z.B. Midea PortaSplit)" required>
                </div>
                <div class="col-md-6">
                    <input type="url" name="url" class="form-control" placeholder="Komplette URL (https://...)" required>
                </div>
                <div class="col-md-2">
                    <button type="submit" class="btn btn-primary w-100">Hinzufügen</button>
                </div>
            </form>
        </div>
    </div>

    <!-- System Einstellungen -->
    <div class="card shadow-sm">
        <div class="card-header bg-secondary text-white">⚙️ System Einstellungen</div>
        <div class="card-body">
            <form action="/save_settings" method="POST">
                <div class="row mb-3">
                    <div class="col-md-6">
                        <label class="form-label">Pushover User Key</label>
                        <input type="text" name="pushover_user" class="form-control" value="{{ config.pushover_user }}" required>
                    </div>
                    <div class="col-md-6">
                        <label class="form-label">Pushover App Token</label>
                        <input type="text" name="pushover_token" class="form-control" value="{{ config.pushover_token }}" required>
                    </div>
                </div>
                <div class="row mb-3">
                    <div class="col-md-6">
                        <label class="form-label">Prüf-Intervall (Minuten)</label>
                        <input type="number" name="interval" class="form-control" value="{{ config.interval }}" min="1" required>
                    </div>
                </div>
                <div class="d-flex justify-content-between">
                    <button type="submit" class="btn btn-secondary">Einstellungen Speichern</button>
                    <a href="/test" class="btn btn-outline-info">Test-Benachrichtigung senden</a>
                </div>
            </form>
        </div>
    </div>

</div>
</body>
</html>
"""

# --- FLASK ROUTES ---
@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE, config=CONFIG, state=STATE, time=time)

@app.route("/save_settings", methods=["POST"])
def save_settings():
    CONFIG["pushover_user"] = request.form["pushover_user"]
    CONFIG["pushover_token"] = request.form["pushover_token"]
    CONFIG["interval"] = int(request.form["interval"])
    save_config()
    return redirect(url_for("index"))

@app.route("/add", methods=["POST"])
def add_item():
    new_item = {
        "id": str(uuid.uuid4())[:8],
        "name": request.form["name"],
        "url": request.form["url"],
        "status": "Wartet auf Check...",
        "last_check": "Noch nie",
        "has_screenshot": False,
        "found_time": 0
    }
    CONFIG["items"].append(new_item)
    save_config()
    return redirect(url_for("index"))

@app.route("/delete/<item_id>")
def delete_item(item_id):
    CONFIG["items"] = [item for item in CONFIG["items"] if item["id"] != item_id]
    
    # Screenshot aufräumen falls vorhanden
    img_path = os.path.join(DATA_DIR, f"screenshot_{item_id}.png")
    if os.path.exists(img_path):
        os.remove(img_path)
        
    save_config()
    return redirect(url_for("index"))

@app.route("/reset_cooldown/<item_id>")
def reset_cooldown(item_id):
    for item in CONFIG["items"]:
        if item["id"] == item_id:
            item["found_time"] = 0
            item["status"] = "Wartet auf Check..."
            save_config()
            break
    return redirect(url_for("index"))

@app.route("/screenshot/<item_id>")
def serve_screenshot(item_id):
    img_path = os.path.join(DATA_DIR, f"screenshot_{item_id}.png")
    if os.path.exists(img_path):
        return send_file(img_path, mimetype='image/png')
    return "Kein Screenshot gefunden", 404

@app.route("/start")
def start():
    global tracker_thread
    if not STATE["is_running"]:
        stop_event.clear()
        tracker_thread = threading.Thread(target=tracker_loop, daemon=True)
        tracker_thread.start()
        STATE["is_running"] = True
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
    send_pushover("Dies ist eine Test-Nachricht. Die Verbindung klappt!", "https://google.com")
    return redirect(url_for("index"))

if __name__ == "__main__":
    load_config()
    app.run(host="0.0.0.0", port=5000)
