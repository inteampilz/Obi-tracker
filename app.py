import os
import json
import time
import uuid
import threading
import requests
import datetime
import random
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, request, render_template_string, redirect, url_for, send_file, jsonify
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

app = Flask(__name__)

DATA_DIR = "data"
DB_FILE = os.path.join(DATA_DIR, "tracker.db")

# Intelligentes Schloss gegen Deadlocks
db_lock = threading.RLock()

STATE = {
    "is_running": False,
    "status": "Gestoppt",
    "current_proxy": "Wartemodus..."
}

SYSTEM_LOGS = []
MAX_LOGS = 200

def log_msg(msg):
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    log_string = f"[{timestamp}] {msg}"
    print(log_string) 
    SYSTEM_LOGS.append(log_string)
    if len(SYSTEM_LOGS) > MAX_LOGS:
        SYSTEM_LOGS.pop(0)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0"
]

tracker_thread = None
stop_event = threading.Event()

# ==========================================
# DATENBANK LOGIK (SQLite)
# ==========================================
def get_db():
    if not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR)
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS items (
            id TEXT PRIMARY KEY, name TEXT, url TEXT, target_price REAL, current_price REAL,
            status TEXT, last_check TEXT, has_screenshot INTEGER, screenshot_time REAL, 
            found_time REAL, is_active INTEGER DEFAULT 1)""")
        
        try: c.execute("ALTER TABLE items ADD COLUMN check_interval INTEGER DEFAULT 15")
        except sqlite3.OperationalError: pass
        
        try: c.execute("ALTER TABLE items ADD COLUMN last_check_ts REAL DEFAULT 0")
        except sqlite3.OperationalError: pass
        
        defaults = {
            "pushover_user": "", "pushover_token": "", "pushover_priority": "0",
            "proxies": "", "proxy_url": "", "require_proxy": "0"
        }
        for k, v in defaults.items():
            c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))
        conn.commit()
        conn.close()

def get_setting(key):
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT value FROM settings WHERE key = ?", (key,))
        res = c.fetchone()
        conn.close()
        return res["value"] if res else ""

def set_setting(key, value):
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE settings SET value = ? WHERE key = ?", (str(value), key))
        conn.commit()
        conn.close()

def get_items():
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT * FROM items ORDER BY is_active DESC, name ASC")
        items = [dict(row) for row in c.fetchall()]
        conn.close()
        return items

def update_item_db(item_id, **kwargs):
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        for k, v in kwargs.items():
            c.execute(f"UPDATE items SET {k} = ? WHERE id = ?", (v, item_id))
        conn.commit()
        conn.close()

# ==========================================
# SYSTEM-LOGIK
# ==========================================
def send_pushover(message, item_url, image_path=None, title="🚨 Produkt-Tracker Alarm"):
    try:
        priority = int(get_setting("pushover_priority") or "0")
        log_msg(f"[SYSTEM] Sende Pushover Benachrichtigung (Priorität: {priority})...")
        data = {
            "token": get_setting("pushover_token"), "user": get_setting("pushover_user"),
            "message": message, "title": title, "url": item_url, "url_title": "Direkt zum Artikel",
            "priority": priority
        }
        if priority == 2:
            data["retry"] = 30
            data["expire"] = 3600

        files = {}
        if image_path and os.path.exists(image_path):
            files["attachment"] = ("screenshot.png", open(image_path, "rb"), "image/png")
        
        if files: requests.post("https://api.pushover.net/1/messages.json", data=data, files=files)
        else: requests.post("https://api.pushover.net/1/messages.json", data=data)
        log_msg(f"[SYSTEM] ✅ Pushover erfolgreich gesendet.")
    except Exception as e:
        log_msg(f"[SYSTEM] ❌ Pushover Fehler: {e}")

def load_proxies():
    proxies = []
    p_text = get_setting("proxies")
    if p_text: 
        manuelle = [p.strip() for p in p_text.split("\n") if p.strip()]
        proxies.extend(manuelle)
        
    api_url = get_setting("proxy_url").strip()
    if api_url:
        try:
            log_msg(f"[PROXY] Lade externe Proxy-Liste herunter...")
            resp = requests.get(api_url, timeout=10)
            found = re.findall(r'[0-9]+(?:\.[0-9]+){3}:[0-9]+', resp.text)
            protocol = "http"
            if "socks5" in api_url.lower(): protocol = "socks5"
            elif "socks4" in api_url.lower(): protocol = "socks4"
            api_proxies = [f"{protocol}://{p}" for p in found]
            proxies.extend(api_proxies)
            log_msg(f"[PROXY] ✅ {len(api_proxies)} {protocol.upper()}-Proxys von API gefunden.")
        except Exception as e: 
            log_msg(f"[PROXY] ⚠️ Fehler beim API-Download: {e}")
            
    final_list = list(set(proxies))
    log_msg(f"[PROXY] Insgesamt {len(final_list)} einzigartige Proxys im Pool verfügbar.")
    return final_list

def setup_driver(proxy=None, item_name="System"):
    log_msg(f"[{item_name}] ⚙️ Konfiguriere Chrome Stealth-Modus...")
    options = Options()
    
    options.add_argument("--headless=new") 
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    
    random_ua = random.choice(USER_AGENTS)
    log_msg(f"[{item_name}] 🎭 Nutze Browser: {random_ua.split(' ')[0]} ...")
    options.add_argument(f"user-agent={random_ua}")
    
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option('useAutomationExtension', False)
    
    if proxy: 
        options.add_argument(f"--proxy-server={proxy}")
        log_msg(f"[{item_name}] 🌐 Nutze Proxy: {proxy}")
    else:
        log_msg(f"[{item_name}] ⚠️ Keine Proxys aktiv (Lokale IP).")
            
    driver = webdriver.Chrome(options=options)
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    })
    return driver

def check_single_item(item, proxy_pool):
    if stop_event.is_set(): return
    
    max_retries = 4 
    success = False
    require_proxy = get_setting("require_proxy") == "1"
    name = item['name']

    if require_proxy and not proxy_pool:
        update_item_db(item["id"], status="⚠️ Fehler: Proxy-Zwang an, keine Proxys.", last_check_ts=time.time())
        log_msg(f"[{name}] 🛑 ABBRUCH: Proxy zwingend, aber Pool ist leer!")
        return
        
    for attempt in range(max_retries):
        if stop_event.is_set(): break
        
        current_proxy = random.choice(proxy_pool) if proxy_pool else None
        if require_proxy and not current_proxy: continue
            
        log_msg(f"[{name}] 🔄 START VERSUCH {attempt+1}/{max_retries}...")
        
        try:
            driver = setup_driver(current_proxy, item_name=name)
            driver.set_page_load_timeout(20) 
            
            log_msg(f"[{name}] 🚀 Rufe URL auf...")
            driver.get(item["url"])
            time.sleep(4)
            
            page_title = driver.title.lower()
            if "403" in page_title or "forbidden" in page_title or "error" in page_title:
                log_msg(f"[{name}] 🚨 CLOUDFRONT/WAF BLOCKIERT! IP wurde als Bot erkannt.")
                raise Exception("CloudFront Block")
            
            try:
                log_msg(f"[{name}] 🍪 Klicke Cookie-Banner...")
                cookie_btn = driver.find_element(By.XPATH, "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'akzeptieren') or contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'zulassen')]")
                driver.execute_script("arguments[0].click();", cookie_btn)
                time.sleep(1)
            except: pass

            log_msg(f"[{name}] 📜 Scrolle und suche nach Verfügbarkeits-Buttons...")
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight/3);")
            time.sleep(2)
            
            try:
                elements = driver.find_elements(By.TAG_NAME, "button") + driver.find_elements(By.TAG_NAME, "a") + driver.find_elements(By.CSS_SELECTOR, "[data-ui-name]")
                clicked = False
                
                for el in elements:
                    text = el.text.lower()
                    ui_name = (el.get_attribute("data-ui-name") or "").lower()
                    
                    if "märkten" in text or "verfügbarkeit" in text or "filiale" in text or "markt prüfen" in text or "availability" in ui_name or "store" in ui_name:
                        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", el)
                        time.sleep(1)
                        driver.execute_script("arguments[0].click();", el)
                        log_msg(f"[{name}] 🔘 Filial-Button geklickt!")
                        clicked = True
                        time.sleep(6) 
                        break
                        
                if not clicked:
                    log_msg(f"[{name}] ℹ️ Kein spezieller Button gefunden.")
            except Exception as e: 
                log_msg(f"[{name}] ⚠️ Fehler beim Button-Klick.")
                
            debug_path = os.path.join(DATA_DIR, f"debug_{item['id']}.png")
            driver.save_screenshot(debug_path)
            log_msg(f"[{name}] 📸 Debug-Screenshot gespeichert.")
            
            log_msg(f"[{name}] 📝 Aktiviere strengen OBI-Textfilter (False-Positive Schutz)...")
            
            raw_html = driver.page_source.lower()
            clean_text = re.sub(r'<(script|style)[^>]*>.*?</\1>', ' ', raw_html, flags=re.IGNORECASE | re.DOTALL)
            clean_text = re.sub(r'<[^>]+>', ' ', clean_text)
            body_text = " ".join(clean_text.split())
            
            is_available = False
            
            # --- 1. TOTSCHLAG-ARGUMENTE (Knockouts) ---
            if ("nichts gefunden" in body_text and "in keinem" in body_text) or "ausverkauft" in body_text:
                log_msg(f"[{name}] 🛑 KNOCKOUT: Popup meldet 'Nichts gefunden' oder Artikel ist 'Ausverkauft'.")
                is_available = False
            else:
                # --- 2. SCHARFE POSITIV-PRÜFUNG ---
                
                # Methode A: Suche nach echtem Filial-Bestand (Sucht z.B. nach "12 stück auf lager")
                # Sucht nach einer Zahl von 1-9999, gefolgt von "stück auf lager". (0 wird komplett ignoriert)
                stock_matches = re.findall(r'([1-9][0-9]*)\s*stück auf lager', body_text)
                
                if stock_matches:
                    log_msg(f"[{name}] 🎯 TREFFER (Filiale): Echter Bestand von {stock_matches[0]} Stück gefunden!")
                    is_available = True
                else:
                    # Methode B: Suche nach klickbarem "In den Warenkorb" Button für reine Online-Käufe
                    try:
                        cart_btns = driver.find_elements(By.XPATH, "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'in den warenkorb')]")
                        for btn in cart_btns:
                            # Prüfen ob der Button wirklich sichtbar und aktiv ist (ignoriert versteckte oder Slider-Buttons)
                            if btn.is_displayed() and btn.is_enabled():
                                log_msg(f"[{name}] 🎯 TREFFER (Online): 'In den Warenkorb' Button ist aktiv!")
                                is_available = True
                                break
                    except: pass
            
            if is_available:
                log_msg(f"[{name}] 🚨 ARTIKEL IST VERFÜGBAR!")
                screenshot_path = os.path.join(DATA_DIR, f"screenshot_{item['id']}.png")
                driver.save_screenshot(screenshot_path)
                update_item_db(item["id"], has_screenshot=1, screenshot_time=time.time(), found_time=time.time(), status="✅ VERFÜGBAR!")
                send_pushover(f"Artikel {name} verfügbar!", item["url"], screenshot_path)
            else:
                log_msg(f"[{name}] ❌ Nicht verfügbar (Keine Bestandszahl gefunden).")
                update_item_db(item["id"], status="❌ Nicht verfügbar.")
                
            driver.quit()
            success = True
            log_msg(f"[{name}] ✨ DURCHLAUF BEENDET.")
            break 
            
        except Exception as e:
            log_msg(f"[{name}] 💥 FEHLER Versuch {attempt+1}: IP/Proxy Blockiert oder Timeout.")
            try: driver.quit()
            except: pass
            if current_proxy and current_proxy in proxy_pool:
                proxy_pool.remove(current_proxy)
    
    if not success:
        log_msg(f"[{name}] 💀 ABBRUCH nach {max_retries} Versuchen.")
        update_item_db(item["id"], status="⚠️ Blockiert (Firewall/Proxys tot)")

def tracker_loop():
    log_msg("[SYSTEM] 🟢 Tracker-Thread gestartet.")
    while not stop_event.is_set():
        items = get_items()
        to_check = [i for i in items if i["is_active"] == 1 and (time.time() - i.get("found_time", 0) > 86400) and (time.time() - i.get("last_check_ts", 0) >= ((i.get("check_interval") or 15) * 60))]
        
        if to_check:
            proxy_pool = load_proxies()
            with ThreadPoolExecutor(max_workers=3) as executor:
                futures = []
                for item in to_check:
                    now_str = datetime.datetime.now().strftime("%d.%m.%Y %H:%M:%S")
                    update_item_db(item["id"], last_check_ts=time.time(), last_check=now_str)
                    futures.append(executor.submit(check_single_item, item, proxy_pool))
                for f in futures:
                    if stop_event.is_set(): break
                    f.result() 
                
        STATE["status"] = "Warte auf nächste Intervalle..."
        stop_event.wait(60) 

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="de">
<head>
    <meta charset="UTF-8">
    <title>Universal Tracker Dashboard</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
        .screenshot-thumb { max-height: 100px; cursor: pointer; transition: 0.3s; }
        .screenshot-thumb:hover { opacity: 0.8; }
        #terminalLog { background-color: #1e1e1e; color: #00ff00; font-family: 'Courier New', monospace; font-size: 13px; resize: none; border: 1px solid #333; line-height: 1.2; }
        .inactive-row { opacity: 0.6; background-color: #f8f9fa; }
    </style>
</head>
<body class="bg-light pb-5">
<div class="container mt-4" style="max-width: 1050px;">
    
    <div class="d-flex justify-content-between align-items-center mb-4">
        <h2>🛒 Universal Tracker <span class="badge bg-danger fs-6 align-middle">ENTERPRISE</span></h2>
        <div>
            {% if not state.is_running %}
                <a href="/start" class="btn btn-success fw-bold">▶ START</a>
            {% else %}
                <a href="/stop" class="btn btn-danger fw-bold">⏸ STOPP</a>
            {% endif %}
        </div>
    </div>

    <!-- Live Terminal -->
    <div class="card shadow-sm mb-4 border-dark">
        <div class="card-header bg-dark text-white d-flex justify-content-between align-items-center py-2">
            <span class="mb-0">📜 Live-Terminal (Very Verbose Mode)</span>
            <span class="spinner-border spinner-border-sm text-success" role="status" aria-hidden="true" style="{{ 'display:none;' if not state.is_running else '' }}"></span>
        </div>
        <div class="card-body p-0">
            <textarea id="terminalLog" class="form-control rounded-0 border-0 p-3" rows="12" readonly>Warte auf Verbindung...</textarea>
        </div>
    </div>

    <!-- Artikel Tabelle -->
    <div class="card shadow-sm mb-4">
        <div class="card-header bg-dark text-white d-flex justify-content-between align-items-center">
            <h5 class="mb-0">📦 Überwachte Artikel</h5>
        </div>
        <div class="card-body p-0 table-responsive">
            <table class="table table-hover mb-0 align-middle">
                <thead class="table-light">
                    <tr>
                        <th>Produkt</th>
                        <th>Status</th>
                        <th>Beweise</th>
                        <th class="text-end">Aktion</th>
                    </tr>
                </thead>
                <tbody>
                    {% for item in items %}
                    <tr class="{{ 'inactive-row' if item.is_active == 0 else '' }}">
                        <td>
                            <strong>{{ item.name }}</strong><br>
                            <a href="{{ item.url }}" target="_blank" class="text-muted small text-decoration-none">🔗 Link öffnen</a><br>
                            <span class="badge bg-secondary mt-1">Intervall: {{ item.check_interval }} Min</span>
                        </td>
                        <td>
                            {% if item.is_active == 0 %}
                                <span class="badge bg-secondary">⏸ Pausiert</span>
                            {% else %}
                                <span class="badge {{ 'bg-success' if 'VERFÜGBAR' in item.status else ('bg-danger' if 'Fehler' in item.status or 'Blockiert' in item.status else 'bg-secondary') }}">
                                    {{ item.status }}
                                </span>
                            {% endif %}
                            <br><small class="text-muted">{{ item.last_check }}</small>
                        </td>
                        <td>
                            {% if item.has_screenshot %}
                                <a href="/screenshot/{{ item.id }}?t={{ item.screenshot_time }}" target="_blank">
                                    <img src="/screenshot/{{ item.id }}?t={{ item.screenshot_time }}" class="screenshot-thumb img-thumbnail mb-1">
                                </a><br>
                            {% endif %}
                            <a href="/debug/{{ item.id }}?t={{ time.time() }}" target="_blank" class="badge bg-info text-decoration-none shadow-sm">📸 Live-Bild</a>
                        </td>
                        <td class="text-end">
                            <div class="btn-group-vertical btn-group-sm">
                                {% if item.is_active == 1 %}
                                    <a href="/toggle/{{ item.id }}" class="btn btn-outline-secondary">⏸ Pause</a>
                                {% else %}
                                    <a href="/toggle/{{ item.id }}" class="btn btn-outline-success">▶ Aktivieren</a>
                                {% endif %}
                                <a href="/reset_cooldown/{{ item.id }}" class="btn btn-outline-warning">Reset / Check Now</a>
                                <a href="/delete/{{ item.id }}" class="btn btn-outline-danger">Löschen</a>
                            </div>
                        </td>
                    </tr>
                    {% else %}
                    <tr><td colspan="4" class="text-center py-4 text-muted">Keine Artikel angelegt.</td></tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
    </div>

    <!-- Artikel hinzufügen -->
    <div class="card shadow-sm mb-4 border-primary">
        <div class="card-header bg-primary text-white">➕ Neuen Artikel hinzufügen</div>
        <div class="card-body">
            <form action="/add" method="POST" class="row g-2">
                <div class="col-md-5"><input type="text" name="name" class="form-control" placeholder="Produktname" required></div>
                <div class="col-md-5"><input type="url" name="url" class="form-control" placeholder="https://..." required></div>
                <div class="col-md-2">
                    <div class="input-group">
                        <input type="number" name="check_interval" class="form-control" placeholder="Intervall" value="15" required>
                        <span class="input-group-text">Min</span>
                    </div>
                </div>
                <div class="col-md-2"><button type="submit" class="btn btn-primary w-100 fw-bold">Hinzufügen</button></div>
            </form>
        </div>
    </div>

    <!-- Settings -->
    <div class="card shadow-sm">
        <div class="card-header bg-secondary text-white">⚙️ System Einstellungen & Proxys</div>
        <div class="card-body">
            <form action="/save_settings" method="POST">
                <div class="row mb-3">
                    <div class="col-md-4"><label class="form-label">Pushover User Key</label><input type="text" name="pushover_user" class="form-control" value="{{ settings.pushover_user }}" required></div>
                    <div class="col-md-4"><label class="form-label">Pushover App Token</label><input type="text" name="pushover_token" class="form-control" value="{{ settings.pushover_token }}" required></div>
                    <div class="col-md-4">
                        <label class="form-label">Pushover Priorität</label>
                        <select name="pushover_priority" class="form-select">
                            <option value="-2" {% if settings.pushover_priority == '-2' %}selected{% endif %}>Stumm (-2)</option>
                            <option value="-1" {% if settings.pushover_priority == '-1' %}selected{% endif %}>Leise (-1)</option>
                            <option value="0" {% if settings.pushover_priority == '0' %}selected{% endif %}>Normal (0)</option>
                            <option value="1" {% if settings.pushover_priority == '1' %}selected{% endif %}>Hoch (1)</option>
                            <option value="2" {% if settings.pushover_priority == '2' %}selected{% endif %}>Notfall (2) - Alarm!</option>
                        </select>
                    </div>
                </div>
                <hr>
                <div class="form-check form-switch mb-3">
                    <input class="form-check-input" type="checkbox" name="require_proxy" id="requireProxy" {% if settings.require_proxy == '1' %}checked{% endif %}>
                    <label class="form-check-label" for="requireProxy"><strong>Proxy zwingend erforderlich</strong></label>
                </div>
                <div class="mb-3"><label class="form-label text-primary"><strong>Proxy API URL</strong></label><input type="url" name="proxy_url" class="form-control" value="{{ settings.proxy_url }}"></div>
                <div class="mb-3"><label class="form-label">Zusätzliche Manuelle Proxys</label><textarea name="proxies" class="form-control font-monospace" rows="2">{{ settings.proxies }}</textarea></div>
                <div class="d-flex justify-content-between mt-3">
                    <button type="submit" class="btn btn-secondary px-5 fw-bold">💾 Speichern</button>
                    <a href="/test" class="btn btn-outline-info">Test-Push senden</a>
                </div>
            </form>
        </div>
    </div>
</div>

<script>
    function updateTerminal() {
        fetch('/api/logs').then(res => res.json()).then(data => {
            const t = document.getElementById('terminalLog');
            const scroll = t.scrollHeight - t.clientHeight <= t.scrollTop + 5;
            t.value = data.length > 0 ? data.join('\\n') : "System bereit.";
            if (scroll) t.scrollTop = t.scrollHeight;
        });
    }
    setInterval(updateTerminal, 1000); updateTerminal();
</script>
</body>
</html>
"""

@app.route("/")
def index():
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT * FROM settings")
        settings_dict = {row["key"]: row["value"] for row in c.fetchall()}
        conn.close()
    items = get_items()
    return render_template_string(HTML_TEMPLATE, settings=settings_dict, items=items, state=STATE, time=time)

@app.route("/api/logs")
def get_logs(): return jsonify(SYSTEM_LOGS)

@app.route("/save_settings", methods=["POST"])
def save_settings_route():
    for k in ["pushover_user", "pushover_token", "pushover_priority", "proxies", "proxy_url"]:
        set_setting(k, request.form.get(k, ""))
    set_setting("require_proxy", "1" if "require_proxy" in request.form else "0")
    log_msg("[SYSTEM] Einstellungen im Web-Dashboard aktualisiert.")
    return redirect(url_for("index"))

@app.route("/add", methods=["POST"])
def add_item_route():
    ci = request.form.get("check_interval", 15)
    item_id = str(uuid.uuid4())[:8]
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("""INSERT INTO items (id, name, url, check_interval, status, is_active, last_check_ts) 
                     VALUES (?, ?, ?, ?, ?, 1, 0)""", 
                  (item_id, request.form["name"], request.form["url"], int(ci), "Wartet auf Check..."))
        conn.commit()
        conn.close()
    return redirect(url_for("index"))

@app.route("/delete/<item_id>")
def delete_item_route(item_id):
    with db_lock:
        conn = get_db()
        conn.cursor().execute("DELETE FROM items WHERE id = ?", (item_id,))
        conn.commit()
        conn.close()
    img = os.path.join(DATA_DIR, f"screenshot_{item_id}.png")
    if os.path.exists(img): os.remove(img)
    return redirect(url_for("index"))

@app.route("/toggle/<item_id>")
def toggle_item_route(item_id):
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT is_active FROM items WHERE id = ?", (item_id,))
        new_status = 0 if c.fetchone()["is_active"] == 1 else 1
        c.execute("UPDATE items SET is_active = ? WHERE id = ?", (new_status, item_id))
        conn.commit()
        conn.close()
    return redirect(url_for("index"))

@app.route("/reset_cooldown/<item_id>")
def reset_cooldown_route(item_id):
    update_item_db(item_id, found_time=0, status="Wartet auf Check...", has_screenshot=0, last_check_ts=0)
    return redirect(url_for("index"))

@app.route("/screenshot/<item_id>")
def serve_screenshot(item_id):
    p = os.path.join(DATA_DIR, f"screenshot_{item_id}.png")
    return send_file(p, mimetype='image/png') if os.path.exists(p) else ("Bild fehlt", 404)

@app.route("/debug/<item_id>")
def serve_debug(item_id):
    p = os.path.join(DATA_DIR, f"debug_{item_id}.png")
    return send_file(p, mimetype='image/png') if os.path.exists(p) else ("Kein Bild", 404)

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
    send_pushover("Test-Nachricht vom Universal Tracker!", "https://google.com")
    return redirect(url_for("index"))

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000)
