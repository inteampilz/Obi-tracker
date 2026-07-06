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
OLD_CONFIG = os.path.join(DATA_DIR, "config.json")

# NEU: Ein intelligentes Schloss, das Deadlocks verhindert!
db_lock = threading.RLock()

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
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/112.0.0.0 Safari/537.36"
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
        c.execute("""CREATE TABLE IF NOT EXISTS price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT, 
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, price REAL)""")
        
        try:
            c.execute("ALTER TABLE items ADD COLUMN check_interval INTEGER DEFAULT 15")
        except sqlite3.OperationalError: pass
        
        try:
            c.execute("ALTER TABLE items ADD COLUMN last_check_ts REAL DEFAULT 0")
        except sqlite3.OperationalError: pass
        
        defaults = {
            "pushover_user": "", "pushover_token": "", "pushover_priority": "0",
            "proxies": "", "proxy_url": "", "require_proxy": "0"
        }
        for k, v in defaults.items():
            c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))
        conn.commit()

        if os.path.exists(OLD_CONFIG):
            try:
                with open(OLD_CONFIG, "r") as f:
                    old_data = json.load(f)
                
                for k in ["pushover_user", "pushover_token", "pushover_priority", "proxies", "proxy_url"]:
                    if k in old_data:
                        c.execute("UPDATE settings SET value = ? WHERE key = ?", (str(old_data[k]), k))
                if "require_proxy" in old_data:
                    c.execute("UPDATE settings SET value = ? WHERE key = ?", ("1" if old_data["require_proxy"] else "0", "require_proxy"))
                
                for item in old_data.get("items", []):
                    c.execute("""INSERT OR IGNORE INTO items 
                        (id, name, url, target_price, current_price, status, last_check, has_screenshot, screenshot_time, found_time, is_active, check_interval, last_check_ts) 
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 15, 0)""", 
                        (item.get("id"), item.get("name"), item.get("url"), item.get("target_price"), 
                         item.get("current_price"), item.get("status"), item.get("last_check"), 
                         1 if item.get("has_screenshot") else 0, item.get("screenshot_time", 0), 
                         item.get("found_time", 0)))
                conn.commit()
                os.rename(OLD_CONFIG, OLD_CONFIG + ".bak")
            except Exception as e:
                log_msg(f"Fehler bei DB Migration: {e}")
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

def log_price_history(item_id, price):
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT price FROM price_history WHERE item_id = ? ORDER BY id DESC LIMIT 1", (item_id,))
        last = c.fetchone()
        if not last or last["price"] != price:
            c.execute("INSERT INTO price_history (item_id, price) VALUES (?, ?)", (item_id, price))
            conn.commit()
        conn.close()

# ==========================================
# SYSTEM-LOGIK
# ==========================================
def send_pushover(message, item_url, image_path=None, title="🚨 Produkt-Tracker Alarm"):
    try:
        priority = int(get_setting("pushover_priority") or "0")
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
    except Exception as e:
        log_msg(f"Pushover Fehler: {e}")

def load_proxies():
    proxies = []
    p_text = get_setting("proxies")
    if p_text: proxies.extend([p.strip() for p in p_text.split("\n") if p.strip()])
        
    api_url = get_setting("proxy_url").strip()
    if api_url:
        try:
            resp = requests.get(api_url, timeout=10)
            found = re.findall(r'[0-9]+(?:\.[0-9]+){3}:[0-9]+', resp.text)
            protocol = "http"
            if "socks5" in api_url.lower(): protocol = "socks5"
            elif "socks4" in api_url.lower(): protocol = "socks4"
            proxies.extend([f"{protocol}://{p}" for p in found])
        except Exception: pass
    return list(set(proxies))

def setup_driver(proxy=None):
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    options.add_argument(f"user-agent={random.choice(USER_AGENTS)}")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option('useAutomationExtension', False)
    if proxy: options.add_argument(f"--proxy-server={proxy}")
            
    driver = webdriver.Chrome(options=options)
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    })
    return driver

def check_single_item(item, proxy_pool):
    if stop_event.is_set(): return
    
    max_retries = 10
    success = False
    require_proxy = get_setting("require_proxy") == "1"

    if require_proxy and not proxy_pool:
        update_item_db(item["id"], status="⚠️ Fehler: Proxy-Zwang an, keine Proxys.", last_check_ts=time.time())
        return
        
    for attempt in range(max_retries):
        if stop_event.is_set(): break
        
        current_proxy = random.choice(proxy_pool) if proxy_pool else None
        if require_proxy and not current_proxy: continue
            
        log_msg(f"Prüfe {item['name']} (Versuch {attempt+1})")
        
        try:
            driver = setup_driver(current_proxy)
            driver.set_page_load_timeout(20) 
            driver.get(item["url"])
            time.sleep(3)
            
            try:
                cookie_btn = driver.find_element(By.XPATH, "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'akzeptieren') or contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'zulassen')]")
                driver.execute_script("arguments[0].click();", cookie_btn)
                time.sleep(1)
            except: pass

            driver.execute_script("window.scrollTo(0, document.body.scrollHeight/3);")

            # --- DER VERBESSERTE PREIS-SCANNER ---
            current_price = None
            try:
                price_el = WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, 'span[data-ui-name="ads.price.strong"]'))
                )
                raw_price = price_el.text.strip()
                clean_p = re.sub(r'[-–—]', '00', raw_price)
                clean_p = clean_p.replace("'", "").replace("’", "")
                
                match = re.search(r'(\d+[\.,]?\d*)', clean_p)
                if match:
                    final_price_str = match.group(1).replace(',', '.')
                    current_price = float(final_price_str)
                    log_msg(f"✅ Preis für {item['name']} fehlerfrei erkannt: {current_price} CHF")
                    
                    update_item_db(item["id"], current_price=current_price)
                    log_price_history(item["id"], current_price)
            except Exception as e:
                log_msg(f"⚠️ Preiselement für {item['name']} nicht gefunden oder nicht lesbar.")
            
            # --- VERFÜGBARKEITS-SCANNER ---
            try:
                buttons = driver.find_elements(By.TAG_NAME, "button") + driver.find_elements(By.TAG_NAME, "a")
                for btn in buttons:
                    text = btn.text.lower()
                    if "märkten" in text or "verfügbarkeit" in text or "filiale" in text:
                        driver.execute_script("arguments[0].scrollIntoView(true);", btn)
                        time.sleep(1)
                        driver.execute_script("arguments[0].click();", btn)
                        time.sleep(4) 
                        break
            except: pass
                
            debug_path = os.path.join(DATA_DIR, f"debug_{item['id']}.png")
            driver.save_screenshot(debug_path)
            
            raw_text = driver.find_element(By.TAG_NAME, "body").text.lower()
            body_text = " ".join(raw_text.split())
            body_text = body_text.replace("keine lieferung", "xxx").replace("keine abholung", "xxx").replace("in keinem markt", "xxx").replace("0 stück", "xxx")
            
            keywords = ["in den warenkorb", "lieferung möglich", "im markt verfügbar", "märkten verfügbar", "stück verfügbar", "stück vorrätig", "reservieren & abholen", "filiale verfügbar"]
            
            is_available = any(k in body_text for k in keywords)
            trigger_type = "none"
            
            if is_available:
                trigger_type = "available"
            elif current_price and item.get("target_price"):
                if current_price <= item["target_price"]:
                    trigger_type = "price_drop"

            if trigger_type != "none":
                screenshot_path = os.path.join(DATA_DIR, f"screenshot_{item['id']}.png")
                driver.save_screenshot(screenshot_path)
                update_item_db(item["id"], has_screenshot=1, screenshot_time=time.time(), found_time=time.time())
                
                if trigger_type == "available":
                    update_item_db(item["id"], status="✅ VERFÜGBAR!")
                    send_pushover(f"Artikel {item['name']} verfügbar!", item["url"], screenshot_path)
                else:
                    update_item_db(item["id"], status=f"📉 PREIS-STURZ ({current_price} CHF)")
                    send_pushover(f"Preis-Alarm! {item['name']} ist auf {current_price} CHF gefallen!", item["url"], screenshot_path, "📉 Preis-Alarm")
            else:
                update_item_db(item["id"], status="❌ Nicht verfügbar / Preis zu hoch.")
                
            driver.quit()
            success = True
            break 
            
        except Exception as e:
            try: driver.quit()
            except: pass
            if current_proxy and current_proxy in proxy_pool:
                proxy_pool.remove(current_proxy)
    
    if not success:
        update_item_db(item["id"], status="⚠️ Fehler (Proxys tot)")

# --- NEU: Minütlicher Check Loop ---
def tracker_loop():
    while not stop_event.is_set():
        STATE["status"] = "Prüfe, ob Artikel fällig sind..."
        
        items = get_items()
        to_check = []
        for i in items:
            if i["is_active"] == 1:
                interval_min = i.get("check_interval") or 15
                if time.time() - i.get("found_time", 0) <= 86400:
                    continue
                if time.time() - i.get("last_check_ts", 0) >= (interval_min * 60):
                    to_check.append(i)
        
        if to_check:
            proxy_pool = load_proxies()
            log_msg(f"Starte Scraping für {len(to_check)} fällige Artikel...")
            
            with ThreadPoolExecutor(max_workers=3) as executor:
                futures = []
                for item in to_check:
                    now_str = datetime.datetime.now().strftime("%d.%m.%Y %H:%M:%S")
                    update_item_db(item["id"], last_check_ts=time.time(), last_check=now_str)
                    futures.append(executor.submit(check_single_item, item, proxy_pool))
                
                for f in futures:
                    if stop_event.is_set(): break
                    f.result() 
                
        STATE["status"] = "Warte auf nächste Intervalle (Checkede minütlich)..."
        stop_event.wait(60) 

# ==========================================
# WEB & DASHBOARD
# ==========================================
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="de">
<head>
    <meta charset="UTF-8">
    <title>Universal Tracker Dashboard</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        .screenshot-thumb { max-height: 100px; cursor: pointer; transition: 0.3s; }
        .screenshot-thumb:hover { opacity: 0.8; }
        #terminalLog { background-color: #1e1e1e; color: #00ff00; font-family: monospace; font-size: 13px; resize: none; border: 1px solid #333; }
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
            <span class="mb-0">📜 Live-Terminal (Bot-Status)</span>
            <span class="spinner-grow spinner-grow-sm text-success" role="status" aria-hidden="true" style="{{ 'display:none;' if not state.is_running else '' }}"></span>
        </div>
        <div class="card-body p-0">
            <textarea id="terminalLog" class="form-control rounded-0 border-0" rows="5" readonly>Warte auf Verbindung...</textarea>
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
                        <th>Preis & Chart</th>
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
                                <span class="badge {{ 'bg-success' if ('VERFÜGBAR' in item.status or 'PREIS-STURZ' in item.status) else ('bg-danger' if 'Fehler' in item.status else 'bg-secondary') }}">
                                    {{ item.status }}
                                </span>
                            {% endif %}
                            <br><small class="text-muted">{{ item.last_check }}</small>
                        </td>
                        <td>
                            <div class="fw-bold">{{ item.current_price if item.current_price else '?' }} CHF</div>
                            {% if item.target_price %}<small class="text-info">Ziel: {{ item.target_price }} CHF</small><br>{% endif %}
                            <button class="btn btn-sm btn-outline-primary mt-1 py-0 px-2" onclick="showChart('{{ item.id }}', '{{ item.name }}')">📈 Graph</button>
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
                                <a href="/reset_cooldown/{{ item.id }}" class="btn btn-outline-warning">Reset</a>
                                <a href="/delete/{{ item.id }}" class="btn btn-outline-danger">Löschen</a>
                            </div>
                        </td>
                    </tr>
                    {% else %}
                    <tr><td colspan="5" class="text-center py-4 text-muted">Keine Artikel angelegt.</td></tr>
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
                <div class="col-md-3"><input type="text" name="name" class="form-control" placeholder="Produktname" required></div>
                <div class="col-md-3"><input type="url" name="url" class="form-control" placeholder="https://..." required></div>
                <div class="col-md-2"><input type="number" step="0.05" name="target_price" class="form-control" placeholder="Preis-Ziel (CHF)"></div>
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

<!-- Chart Modal -->
<div class="modal fade" id="chartModal" tabindex="-1">
  <div class="modal-dialog modal-lg">
    <div class="modal-content">
      <div class="modal-header">
        <h5 class="modal-title">Preisverlauf: <span id="chartItemName"></span></h5>
        <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
      </div>
      <div class="modal-body">
        <canvas id="priceChart" width="400" height="200"></canvas>
      </div>
    </div>
  </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
<script>
    function updateTerminal() {
        fetch('/api/logs').then(res => res.json()).then(data => {
            const t = document.getElementById('terminalLog');
            const scroll = t.scrollHeight - t.clientHeight <= t.scrollTop + 1;
            t.value = data.length > 0 ? data.join('\\n') : "System bereit.";
            if (scroll) t.scrollTop = t.scrollHeight;
        });
    }
    setInterval(updateTerminal, 2000); updateTerminal();

    let myChart = null;
    const chartModal = new bootstrap.Modal(document.getElementById('chartModal'));
    
    function showChart(itemId, itemName) {
        document.getElementById('chartItemName').innerText = itemName;
        fetch('/api/history/' + itemId).then(res => res.json()).then(data => {
            const ctx = document.getElementById('priceChart').getContext('2d');
            if (myChart) myChart.destroy();
            myChart = new Chart(ctx, {
                type: 'line',
                data: {
                    labels: data.map(d => d.date),
                    datasets: [{
                        label: 'Preis (CHF)',
                        data: data.map(d => d.price),
                        borderColor: 'rgb(75, 192, 192)',
                        tension: 0.1, fill: true, backgroundColor: 'rgba(75, 192, 192, 0.2)'
                    }]
                },
                options: { scales: { y: { beginAtZero: false } } }
            });
            chartModal.show();
        });
    }
</script>
</body>
</html>
"""

# ==========================================
# WICHTIG: Die getrennten Datenbank-Abfragen, 
# damit sich die Schlösser nicht mehr blockieren!
# ==========================================
@app.route("/")
def index():
    # 1. Wir holen die Settings (Schloss öffnet und schließt sich)
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT * FROM settings")
        settings_dict = {row["key"]: row["value"] for row in c.fetchall()}
        conn.close()
        
    # 2. Wir holen die Artikel (Schloss öffnet und schließt sich separat)
    items = get_items()
    
    return render_template_string(HTML_TEMPLATE, settings=settings_dict, items=items, state=STATE, time=time)

@app.route("/api/logs")
def get_logs(): return jsonify(SYSTEM_LOGS)

@app.route("/api/history/<item_id>")
def get_history(item_id):
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT timestamp, price FROM price_history WHERE item_id = ? ORDER BY timestamp ASC", (item_id,))
        rows = [{"date": row["timestamp"].split(" ")[0], "price": row["price"]} for row in c.fetchall()]
        conn.close()
    return jsonify(rows)

@app.route("/save_settings", methods=["POST"])
def save_settings_route():
    for k in ["pushover_user", "pushover_token", "pushover_priority", "proxies", "proxy_url"]:
        set_setting(k, request.form.get(k, ""))
    set_setting("require_proxy", "1" if "require_proxy" in request.form else "0")
    log_msg("System-Einstellungen gespeichert.")
    return redirect(url_for("index"))

@app.route("/add", methods=["POST"])
def add_item_route():
    tp = request.form.get("target_price")
    ci = request.form.get("check_interval", 15)
    item_id = str(uuid.uuid4())[:8]
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("""INSERT INTO items (id, name, url, target_price, check_interval, status, is_active, last_check_ts) 
                     VALUES (?, ?, ?, ?, ?, ?, 1, 0)""", 
                  (item_id, request.form["name"], request.form["url"], float(tp) if tp else None, int(ci), "Wartet auf Check..."))
        conn.commit()
        conn.close()
    log_msg(f"Neuer Artikel hinzugefügt: {request.form['name']} (Intervall: {ci} Min)")
    return redirect(url_for("index"))

@app.route("/delete/<item_id>")
def delete_item_route(item_id):
    with db_lock:
        conn = get_db()
        conn.cursor().execute("DELETE FROM items WHERE id = ?", (item_id,))
        conn.cursor().execute("DELETE FROM price_history WHERE item_id = ?", (item_id,))
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
        current = c.fetchone()["is_active"]
        c.execute("UPDATE items SET is_active = ? WHERE id = ?", (0 if current == 1 else 1, item_id))
        conn.commit()
        conn.close()
    return redirect(url_for("index"))

@app.route("/reset_cooldown/<item_id>")
def reset_cooldown_route(item_id):
    update_item_db(item_id, found_time=0, status="Wartet auf Check...", has_screenshot=0)
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
        log_msg("Tracker manuell GESTARTET.")
    return redirect(url_for("index"))

@app.route("/stop")
def stop():
    if STATE["is_running"]:
        stop_event.set()
        STATE["is_running"] = False
        STATE["status"] = "Gestoppt"
        log_msg("Tracker manuell GESTOPPT.")
    return redirect(url_for("index"))

@app.route("/test")
def test_push():
    send_pushover("Test-Nachricht vom Universal Tracker!", "https://google.com")
    return redirect(url_for("index"))

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000)
