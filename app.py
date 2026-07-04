import os
import json
import time
import uuid
import threading
import requests
import datetime
import random
import re
from flask import Flask, request, render_template_string, redirect, url_for, send_file, jsonify
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

app = Flask(__name__)

DATA_DIR = "data"
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
config_lock = threading.Lock()

CONFIG = {
    "pushover_user": os.getenv("PUSHOVER_USER_KEY", ""),
    "pushover_token": os.getenv("PUSHOVER_APP_TOKEN", ""),
    "pushover_priority": 0, # NEU: Standard-Priorität
    "interval": 15,
    "proxies": "", 
    "proxy_url": "",
    "require_proxy": False,
    "items": []
}

STATE = {
    "is_running": False,
    "status": "Gestoppt",
    "current_proxy": "Wartemodus..."
}

SYSTEM_LOGS = []
MAX_LOGS = 50

def log_msg(msg):
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    log_string = f"[{timestamp}] {msg}"
    print(log_string) 
    SYSTEM_LOGS.append(log_string)
    if len(SYSTEM_LOGS) > MAX_LOGS:
        SYSTEM_LOGS.pop(0)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/113.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/112.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/114.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:109.0) Gecko/20100101 Firefox/113.0"
]

tracker_thread = None
stop_event = threading.Event()

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
                log_msg(f"Fehler beim Laden der Config: {e}")
    else:
        save_config()

def save_config():
    init_data_dir()
    with config_lock:
        with open(CONFIG_FILE, "w") as f:
            json.dump(CONFIG, f, indent=4)

def send_pushover(message, item_url, image_path=None, title="🚨 Produkt-Tracker Alarm"):
    try:
        priority = int(CONFIG.get("pushover_priority", 0))
        data = {
            "token": CONFIG["pushover_token"],
            "user": CONFIG["pushover_user"],
            "message": message,
            "title": title,
            "url": item_url,
            "url_title": "Direkt zum Artikel",
            "priority": priority
        }
        
        # Für Notfall-Priorität 2 verlangt Pushover zwingend retry/expire Werte!
        if priority == 2:
            data["retry"] = 30 # Alle 30 Sekunden wiederholen
            data["expire"] = 3600 # Nach 1 Stunde aufhören

        files = {}
        if image_path and os.path.exists(image_path):
            files["attachment"] = ("screenshot.png", open(image_path, "rb"), "image/png")
        
        if files:
            requests.post("https://api.pushover.net/1/messages.json", data=data, files=files)
        else:
            requests.post("https://api.pushover.net/1/messages.json", data=data)
            
        log_msg(f"Pushover-Nachricht (Prio: {priority}) erfolgreich gesendet!")
    except Exception as e:
        log_msg(f"Pushover Fehler: {e}")

def load_proxies():
    proxies = []
    if CONFIG.get("proxies"):
        proxies.extend([p.strip() for p in CONFIG["proxies"].split("\n") if p.strip()])
        
    api_url = CONFIG.get("proxy_url", "").strip()
    if api_url:
        try:
            log_msg("Lade Proxy-Liste von API herunter...")
            resp = requests.get(api_url, timeout=10)
            
            if "geonode.com" in api_url:
                data = resp.json().get("data", [])
                for p in data:
                    protocols = p.get("protocols", ["http"])
                    proto = protocols[0] if protocols else "http"
                    ip = p.get("ip")
                    port = p.get("port")
                    if ip and port:
                        proxies.append(f"{proto}://{ip}:{port}")
            else:
                found = re.findall(r'[0-9]+(?:\.[0-9]+){3}:[0-9]+', resp.text)
                protocol = "http"
                if "socks5" in api_url.lower(): protocol = "socks5"
                elif "socks4" in api_url.lower(): protocol = "socks4"
                proxies.extend([f"{protocol}://{p}" for p in found])
                
            log_msg(f"{len(proxies)} Proxys erfolgreich geladen.")
        except Exception as e:
            log_msg(f"Fehler beim Proxy-Download: {e}")
            
    return list(set(proxies))

def setup_driver(proxy=None):
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    
    random_ua = random.choice(USER_AGENTS)
    options.add_argument(f"user-agent={random_ua}")
    
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option('useAutomationExtension', False)
    
    if proxy:
        options.add_argument(f"--proxy-server={proxy}")
            
    driver = webdriver.Chrome(options=options)
    
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    })
    
    return driver

def check_item(driver, item):
    log_msg(f"Öffne URL für: {item['name']}")
    driver.get(item["url"])
    time.sleep(5) 
    
    try:
        cookie_btn = driver.find_element(By.XPATH, "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'akzeptieren') or contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'zulassen')]")
        driver.execute_script("arguments[0].click();", cookie_btn)
        log_msg("Cookie-Banner weggeklickt.")
        time.sleep(1)
    except:
        pass

    driver.execute_script("window.scrollTo(0, document.body.scrollHeight/3);")
    time.sleep(2)

    try:
        buttons = driver.find_elements(By.TAG_NAME, "button") + driver.find_elements(By.TAG_NAME, "a")
        for btn in buttons:
            text = btn.text.lower()
            if "märkten" in text or "verfügbarkeit" in text or "filiale" in text:
                driver.execute_script("arguments[0].scrollIntoView(true);", btn)
                time.sleep(1)
                driver.execute_script("arguments[0].click();", btn)
                log_msg("Filial-Menü geöffnet.")
                time.sleep(4) 
                break
    except:
        pass
        
    debug_path = os.path.join(DATA_DIR, f"debug_{item['id']}.png")
    driver.save_screenshot(debug_path)
    
    raw_text = driver.find_element(By.TAG_NAME, "body").text.lower()
    
    # --- VERBESSERTE PREIS-EXTRAKTION ---
    try:
        prices = re.findall(r'chf\s*([0-9\']+[.,]?[0-9]*)', raw_text)
        valid_prices = []
        for p in prices:
            clean_p = p.replace("'", "").replace("’", "").replace(",", ".")
            try:
                val = float(clean_p)
                if 1.0 < val < 20000.0:
                    valid_prices.append(val)
            except:
                continue
        
        if valid_prices:
            item["current_price"] = valid_prices[0]
            log_msg(f"✅ Preis erkannt: {item['current_price']} CHF")
        else:
            log_msg("⚠️ Kein valider Preis gefunden.")
    except Exception as e:
        log_msg(f"Fehler bei Preis-Extraktion: {e}")

    body_text = " ".join(raw_text.split())
    
    body_text = body_text.replace("keine lieferung", "xxx").replace("keine abholung", "xxx")
    body_text = body_text.replace("in keinem markt", "xxx").replace("nicht reservierbar", "xxx")
    body_text = body_text.replace("0 stück", "xxx").replace("momentan nicht", "xxx")
    
    keywords = [
        "in den warenkorb", "lieferung möglich", "im markt verfügbar", "märkten verfügbar",
        "stück verfügbar", "stück auf lager", "stück vorrätig", "reservieren & abholen",
        "marktabholung", "abholung im markt", "abholbereit", "zur abholung", 
        "markt abholbar", "märkten abholbar", "filiale verfügbar"
    ]
    
    is_available = False
    for keyword in keywords:
        if keyword in body_text:
            is_available = True
            log_msg(f"Treffer für Verfügbarkeit: '{keyword}'")
            break
            
    if is_available:
        screenshot_path = os.path.join(DATA_DIR, f"screenshot_{item['id']}.png")
        driver.save_screenshot(screenshot_path)
        item["has_screenshot"] = True
        item["screenshot_time"] = time.time()
        return True, "available"
        
    if item.get("current_price") and item.get("target_price"):
        if item["current_price"] <= item["target_price"]:
            screenshot_path = os.path.join(DATA_DIR, f"screenshot_{item['id']}.png")
            driver.save_screenshot(screenshot_path)
            item["has_screenshot"] = True
            item["screenshot_time"] = time.time()
            log_msg("Preis-Limit unterschritten!")
            return True, "price_drop"

    return False, "none"

def tracker_loop():
    while not stop_event.is_set():
        STATE["status"] = "Prüfe Artikel..."
        
        needs_check = False
        for item in CONFIG["items"]:
            if time.time() - item.get("found_time", 0) > 86400:
                needs_check = True
                
        if needs_check and CONFIG["items"]:
            proxy_pool = load_proxies()
            
            for item in CONFIG["items"]:
                if stop_event.is_set(): break
                if time.time() - item.get("found_time", 0) <= 86400: continue
                    
                item["last_check"] = datetime.datetime.now().strftime("%d.%m.%Y %H:%M:%S")
                
                if CONFIG.get("require_proxy") and not proxy_pool:
                    item["status"] = "⚠️ Fehler: Kein Proxy vorhanden!"
                    log_msg("Abbruch: 'Proxy-Zwang' ist an, aber keine Proxys geladen.")
                    save_config()
                    continue
                
                max_retries = 10
                success = False
                
                for attempt in range(max_retries):
                    if stop_event.is_set(): break
                    
                    current_proxy = random.choice(proxy_pool) if proxy_pool else None
                    if CONFIG.get("require_proxy") and not current_proxy: continue
                        
                    STATE["current_proxy"] = current_proxy if current_proxy else "Lokale Server-IP (Kein Proxy)"
                    log_msg(f"Start Check (Versuch {attempt+1}) mit Proxy: {STATE['current_proxy']}")
                    
                    try:
                        driver = setup_driver(current_proxy)
                        driver.set_page_load_timeout(12) 
                        
                        trigger, trigger_type = check_item(driver, item)
                        
                        if trigger:
                            item["found_time"] = time.time()
                            img_path = os.path.join(DATA_DIR, f"screenshot_{item['id']}.png")
                            
                            if trigger_type == "available":
                                item["status"] = "✅ VERFÜGBAR!"
                                send_pushover(f"Der Artikel {item['name']} ist jetzt verfügbar!", item["url"], img_path)
                            elif trigger_type == "price_drop":
                                item["status"] = f"📉 PREIS-STURZ ({item['current_price']} CHF)"
                                send_pushover(f"Preis-Alarm! {item['name']} ist auf {item['current_price']} CHF gefallen!", item["url"], img_path, title="📉 Preis-Alarm")
                        else:
                            item["status"] = "❌ Nicht verfügbar / Preis zu hoch."
                            
                        save_config()
                        driver.quit()
                        success = True
                        log_msg(f"Check für {item['name']} erfolgreich abgeschlossen.")
                        break 
                        
                    except Exception as e:
                        log_msg(f"Proxy Timeout/Fehler bei {STATE['current_proxy']} - Überspringe.")
                        try: driver.quit()
                        except: pass
                        if current_proxy and current_proxy in proxy_pool:
                            proxy_pool.remove(current_proxy)
                
                if not success:
                    item["status"] = "⚠️ Fehler (Proxys tot)"
                    log_msg(f"Alle Proxys für {item['name']} fehlgeschlagen.")
                    save_config()
                
        STATE["status"] = f"Warte {CONFIG['interval']} Min..."
        STATE["current_proxy"] = "Schlafmodus..."
        log_msg(f"Gehe schlafen für {CONFIG['interval']} Minuten...")
        stop_event.wait(CONFIG["interval"] * 60)

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
        #terminalLog { background-color: #1e1e1e; color: #00ff00; font-family: monospace; font-size: 13px; resize: none; border: 1px solid #333; }
    </style>
</head>
<body class="bg-light pb-5">
<div class="container mt-4" style="max-width: 900px;">
    
    <div class="d-flex justify-content-between align-items-center mb-4">
        <h2>🛒 Universal Tracker <span class="badge bg-danger fs-6 align-middle">PRO</span></h2>
        <div>
            {% if not state.is_running %}
                <a href="/start" class="btn btn-success">▶ Tracker Starten</a>
            {% else %}
                <a href="/stop" class="btn btn-danger">⏸ Tracker Stoppen</a>
            {% endif %}
        </div>
    </div>

    <!-- Status -->
    <div class="alert alert-{{ 'success' if state.is_running else 'secondary' }} d-flex justify-content-between align-items-center shadow-sm">
        <div>
            <div><strong>System-Status:</strong> {{ state.status }}</div>
            <div class="mt-2"><strong>Aktiver Proxy:</strong> <span class="badge bg-dark font-monospace fs-6">{{ state.current_proxy }}</span></div>
        </div>
        <div class="text-end">
            Tracker ist <strong>{{ 'AKTIV' if state.is_running else 'GESTOPPT' }}</strong>
        </div>
    </div>

    <!-- Live Terminal -->
    <div class="card shadow-sm mb-4 border-dark">
        <div class="card-header bg-dark text-white d-flex justify-content-between align-items-center py-2">
            <span class="mb-0">📜 Live-Terminal (Bot-Status)</span>
            <span class="spinner-grow spinner-grow-sm text-success" role="status" aria-hidden="true" style="{{ 'display:none;' if not state.is_running else '' }}"></span>
        </div>
        <div class="card-body p-0">
            <textarea id="terminalLog" class="form-control rounded-0 border-0" rows="6" readonly>Warte auf Verbindung...</textarea>
        </div>
    </div>

    <div class="card shadow-sm mb-4">
        <div class="card-header bg-dark text-white">
            <h5 class="mb-0">📦 Überwachte Artikel ({{ config['items']|length }})</h5>
        </div>
        <div class="card-body p-0">
            <table class="table table-hover mb-0">
                <thead class="table-light">
                    <tr>
                        <th>Produkt</th>
                        <th>Preis & Status</th>
                        <th>Beweis & Kamera</th>
                        <th class="text-end">Aktion</th>
                    </tr>
                </thead>
                <tbody>
                    {% for item in config['items'] %}
                    <tr>
                        <td>
                            <strong>{{ item.name }}</strong><br>
                            <a href="{{ item.url }}" target="_blank" class="text-muted small">🔗 Zum Shop</a><br>
                            {% if item.target_price %}
                                <span class="badge bg-info mt-1">Wunschpreis: {{ item.target_price }} CHF</span>
                            {% endif %}
                        </td>
                        <td>
                            <span class="badge {{ 'bg-success' if ('VERFÜGBAR' in item.status or 'PREIS-STURZ' in item.status) else ('bg-danger' if 'Fehler' in item.status else 'bg-secondary') }}">
                                {{ item.status }}
                            </span><br>
                            <small class="text-muted">Check: {{ item.last_check }}</small>
                        </td>
                        <td>
                            {% if item.has_screenshot %}
                                <a href="/screenshot/{{ item.id }}?t={{ item.screenshot_time }}" target="_blank">
                                    <img src="/screenshot/{{ item.id }}?t={{ item.screenshot_time }}" class="screenshot-thumb img-thumbnail mb-1">
                                </a><br>
                            {% endif %}
                            <a href="/debug/{{ item.id }}?t={{ time.time() }}" target="_blank" class="badge bg-info text-decoration-none py-2 px-3 mt-1 shadow-sm">
                                📸 Bot-Kamera
                            </a>
                        </td>
                        <td class="text-end">
                            {% if item.found_time and (time.time() - item.found_time) < 86400 %}
                                <a href="/reset_cooldown/{{ item.id }}" class="btn btn-sm btn-outline-warning mb-1">Cooldown Reset</a><br>
                            {% endif %}
                            <a href="/delete/{{ item.id }}" class="btn btn-sm btn-outline-danger">Löschen</a>
                        </td>
                    </tr>
                    {% else %}
                    <tr><td colspan="4" class="text-center py-4 text-muted">Keine Artikel angelegt.</td></tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
    </div>

    <div class="card shadow-sm mb-4 border-primary">
        <div class="card-header bg-primary text-white">➕ Neuen Artikel hinzufügen</div>
        <div class="card-body">
            <form action="/add" method="POST" class="row g-2">
                <div class="col-md-3">
                    <input type="text" name="name" class="form-control" placeholder="Produktname" required>
                </div>
                <div class="col-md-5">
                    <input type="url" name="url" class="form-control" placeholder="https://..." required>
                </div>
                <div class="col-md-2">
                    <input type="number" step="0.05" name="target_price" class="form-control" placeholder="Preis-Alarm (CHF)">
                </div>
                <div class="col-md-2">
                    <button type="submit" class="btn btn-primary w-100">Hinzufügen</button>
                </div>
            </form>
        </div>
    </div>

    <!-- System Einstellungen & Proxys -->
    <div class="card shadow-sm">
        <div class="card-header bg-secondary text-white">⚙️ System Einstellungen & Proxys</div>
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
                    <!-- NEU: PUSHOVER PRIORITÄT -->
                    <div class="col-md-6">
                        <label class="form-label">Pushover Priorität</label>
                        <select name="pushover_priority" class="form-select">
                            <option value="-2" {% if config.pushover_priority == -2 %}selected{% endif %}>Stumm (-2)</option>
                            <option value="-1" {% if config.pushover_priority == -1 %}selected{% endif %}>Leise (-1)</option>
                            <option value="0" {% if config.pushover_priority == 0 %}selected{% endif %}>Normal (0)</option>
                            <option value="1" {% if config.pushover_priority == 1 %}selected{% endif %}>Hoch (1) - Ignoriert Ruhezeiten</option>
                            <option value="2" {% if config.pushover_priority == 2 %}selected{% endif %}>Notfall (2) - Klingelt bis zur Bestätigung!</option>
                        </select>
                    </div>
                </div>
                <hr>
                <div class="form-check form-switch mb-3">
                    <input class="form-check-input" type="checkbox" name="require_proxy" id="requireProxy" {% if config.require_proxy %}checked{% endif %}>
                    <label class="form-check-label" for="requireProxy"><strong>Proxy zwingend erforderlich</strong> (Sicherheits-Netzwerk)</label>
                </div>
                <div class="mb-3">
                    <label class="form-label text-primary"><strong>Proxy API URL (z.B. GitHub)</strong></label>
                    <input type="url" name="proxy_url" class="form-control" value="{{ config.proxy_url|default('', true) }}">
                </div>
                <div class="mb-3">
                    <label class="form-label">Zusätzliche Manuelle Proxys (Optional – Ein Proxy pro Zeile)</label>
                    <textarea name="proxies" class="form-control font-monospace" rows="3">{{ config.proxies }}</textarea>
                </div>
                
                <div class="d-flex justify-content-between mt-4">
                    <button type="submit" class="btn btn-secondary px-5">💾 Einstellungen Speichern</button>
                    <a href="/test" class="btn btn-outline-info">Test-Push senden</a>
                </div>
            </form>
        </div>
    </div>
</div>

<script>
    function updateTerminal() {
        fetch('/api/logs')
            .then(response => response.json())
            .then(data => {
                const terminal = document.getElementById('terminalLog');
                const isScrolledToBottom = terminal.scrollHeight - terminal.clientHeight <= terminal.scrollTop + 1;
                
                if (data.length > 0) {
                    terminal.value = data.join('\\n');
                } else {
                    terminal.value = "Terminal bereit. Warte auf System-Start...";
                }
                
                if (isScrolledToBottom) {
                    terminal.scrollTop = terminal.scrollHeight;
                }
            })
            .catch(err => console.error("Terminal Update Fehler:", err));
    }
    setInterval(updateTerminal, 2000);
    updateTerminal();
</script>
</body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE, config=CONFIG, state=STATE, time=time)

@app.route("/api/logs")
def get_logs():
    return jsonify(SYSTEM_LOGS)

@app.route("/save_settings", methods=["POST"])
def save_settings():
    CONFIG["pushover_user"] = request.form["pushover_user"]
    CONFIG["pushover_token"] = request.form["pushover_token"]
    CONFIG["interval"] = int(request.form["interval"])
    CONFIG["pushover_priority"] = int(request.form.get("pushover_priority", 0))
    CONFIG["proxies"] = request.form.get("proxies", "")
    CONFIG["proxy_url"] = request.form.get("proxy_url", "")
    CONFIG["require_proxy"] = "require_proxy" in request.form
    save_config()
    log_msg(f"Einstellungen gespeichert. Priorität jetzt auf {CONFIG['pushover_priority']}.")
    return redirect(url_for("index"))

@app.route("/add", methods=["POST"])
def add_item():
    target_price = request.form.get("target_price")
    target_price = float(target_price) if target_price else None
    
    new_item = {
        "id": str(uuid.uuid4())[:8],
        "name": request.form["name"],
        "url": request.form["url"],
        "target_price": target_price,
        "current_price": None,
        "status": "Wartet auf Check...",
        "last_check": "Noch nie",
        "has_screenshot": False,
        "found_time": 0
    }
    CONFIG["items"].append(new_item)
    save_config()
    log_msg(f"Neuer Artikel hinzugefügt: {new_item['name']}")
    return redirect(url_for("index"))

@app.route("/delete/<item_id>")
def delete_item(item_id):
    CONFIG["items"] = [item for item in CONFIG["items"] if item["id"] != item_id]
    img_path = os.path.join(DATA_DIR, f"screenshot_{item_id}.png")
    if os.path.exists(img_path): os.remove(img_path)
    save_config()
    return redirect(url_for("index"))

@app.route("/reset_cooldown/<item_id>")
def reset_cooldown(item_id):
    for item in CONFIG["items"]:
        if item["id"] == item_id:
            item["found_time"] = 0
            item["status"] = "Wartet auf Check..."
            item["has_screenshot"] = False
            save_config()
            log_msg(f"Cooldown für {item['name']} zurückgesetzt.")
            break
    return redirect(url_for("index"))

@app.route("/screenshot/<item_id>")
def serve_screenshot(item_id):
    img_path = os.path.join(DATA_DIR, f"screenshot_{item_id}.png")
    return send_file(img_path, mimetype='image/png') if os.path.exists(img_path) else ("Bild fehlt", 404)

@app.route("/debug/<item_id>")
def serve_debug(item_id):
    img_path = os.path.join(DATA_DIR, f"debug_{item_id}.png")
    return send_file(img_path, mimetype='image/png') if os.path.exists(img_path) else ("Kein Bild", 404)

@app.route("/start")
def start():
    global tracker_thread
    if not STATE["is_running"]:
        stop_event.clear()
        tracker_thread = threading.Thread(target=tracker_loop, daemon=True)
        tracker_thread.start()
        STATE["is_running"] = True
        log_msg("Tracker manuell GESTARTET.")
    return redirect(url_for("index"))

@app.route("/stop")
def stop():
    if STATE["is_running"]:
        stop_event.set()
        STATE["is_running"] = False
        STATE["status"] = "Gestoppt"
        STATE["current_proxy"] = "Gestoppt"
        log_msg("Tracker manuell GESTOPPT.")
    return redirect(url_for("index"))

@app.route("/test")
def test_push():
    send_pushover("Test-Nachricht vom Tracker PRO!", "https://google.com")
    return redirect(url_for("index"))

if __name__ == "__main__":
    load_config()
    app.run(host="0.0.0.0", port=5000)
