import os
import json
import time
import uuid
import threading
import requests
import datetime
import random
import re
from flask import Flask, request, render_template_string, redirect, url_for, send_file
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
    "interval": 15,
    "proxies": "", 
    "proxy_url": "",
    "require_proxy": False, # NEU: Standardmäßig aus
    "items": []
}

STATE = {
    "is_running": False,
    "status": "Gestoppt",
    "current_proxy": "Wartemodus..."
}

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
                print(f"Fehler beim Laden der Config: {e}")
    else:
        save_config()

def save_config():
    init_data_dir()
    with config_lock:
        with open(CONFIG_FILE, "w") as f:
            json.dump(CONFIG, f, indent=4)

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

def load_proxies():
    proxies = []
    if CONFIG.get("proxies"):
        proxies.extend([p.strip() for p in CONFIG["proxies"].split("\n") if p.strip()])
        
    api_url = CONFIG.get("proxy_url", "").strip()
    if api_url:
        try:
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
                proxies.extend([f"http://{p}" for p in found])
        except Exception as e:
            print(f"Fehler beim Proxy-Download: {e}")
    return list(set(proxies))

def setup_driver(proxy=None):
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36")
    
    if proxy:
        options.add_argument(f"--proxy-server={proxy}")
            
    return webdriver.Chrome(options=options)

def check_item(driver, item):
    driver.get(item["url"])
    time.sleep(5) 
    
    try:
        cookie_btn = driver.find_element(By.XPATH, "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'akzeptieren') or contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'zulassen')]")
        driver.execute_script("arguments[0].click();", cookie_btn)
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
                time.sleep(4) 
                break
    except:
        pass
        
    debug_path = os.path.join(DATA_DIR, f"debug_{item['id']}.png")
    driver.save_screenshot(debug_path)
    
    raw_text = driver.find_element(By.TAG_NAME, "body").text.lower()
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
    
    for keyword in keywords:
        if keyword in body_text:
            screenshot_path = os.path.join(DATA_DIR, f"screenshot_{item['id']}.png")
            driver.save_screenshot(screenshot_path)
            item["has_screenshot"] = True
            item["screenshot_time"] = time.time()
            return True
    return False

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
                
                # Logic for mandatory proxy
                if CONFIG.get("require_proxy") and not proxy_pool:
                    item["status"] = "⚠️ Fehler: Kein Proxy"
                    save_config()
                    continue
                
                success = False
                for attempt in range(3):
                    if stop_event.is_set(): break
                    
                    current_proxy = random.choice(proxy_pool) if proxy_pool else None
                    
                    # Wenn Proxy zwingend, dann Abbruch falls kein Proxy gewählt
                    if CONFIG.get("require_proxy") and not current_proxy: continue
                    
                    STATE["current_proxy"] = current_proxy if current_proxy else "Lokale IP"
                    
                    driver = setup_driver(current_proxy)
                    driver.set_page_load_timeout(20)
                    
                    try:
                        is_available = check_item(driver, item)
                        item["status"] = "✅ VERFÜGBAR!" if is_available else "❌ Nicht verfügbar."
                        if is_available:
                            item["found_time"] = time.time()
                            send_pushover(f"🚨 ALARM! {item['name']} ist verfügbar!", item["url"], os.path.join(DATA_DIR, f"screenshot_{item['id']}.png"))
                        save_config()
                        driver.quit()
                        success = True
                        break
                    except Exception:
                        driver.quit()
                        if current_proxy and current_proxy in proxy_pool: proxy_pool.remove(current_proxy)
                
                if not success:
                    item["status"] = "⚠️ Proxy-Fehler"
                    save_config()
                
        STATE["status"] = f"Warte {CONFIG['interval']} Min..."
        STATE["current_proxy"] = "Schlafmodus..."
        stop_event.wait(CONFIG["interval"] * 60)

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="de">
<head><meta charset="UTF-8"><title>Universal Tracker</title><link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet"></head>
<body class="bg-light pb-5"><div class="container mt-4" style="max-width: 900px;">
    <h2>🛒 Universal Produkt-Tracker</h2>
    <div class="alert alert-secondary mt-3">
        <strong>Aktiver Proxy:</strong> <span class="badge bg-dark">{{ state.current_proxy }}</span> | 
        <strong>System:</strong> {{ state.status }}
    </div>
    <div class="card mb-4"><div class="card-body p-0">
        <table class="table table-hover mb-0">
            <thead><tr><th>Produkt</th><th>Status</th><th>Kamera</th><th>Aktion</th></tr></thead>
            <tbody>
                {% for item in config['items'] %}
                <tr>
                    <td><strong>{{ item.name }}</strong><br><a href="{{ item.url }}" target="_blank">🔗</a></td>
                    <td><span class="badge {{ 'bg-success' if 'VERFÜGBAR' in item.status else 'bg-secondary' }}">{{ item.status }}</span></td>
                    <td><a href="/debug/{{ item.id }}" class="badge bg-info">📸</a></td>
                    <td><a href="/delete/{{ item.id }}" class="btn btn-sm btn-danger">Löschen</a></td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </div></div>

    <div class="card shadow-sm"><div class="card-header bg-secondary text-white">⚙️ Proxy Einstellungen</div>
    <div class="card-body"><form action="/save_settings" method="POST">
        <div class="form-check form-switch mb-3">
            <input class="form-check-input" type="checkbox" name="require_proxy" id="requireProxy" {% if config.require_proxy %}checked{% endif %}>
            <label class="form-check-label" for="requireProxy"><strong>Proxy zwingend erforderlich (Scraping ohne IP-Blockade)</strong></label>
        </div>
        <div class="mb-3"><label class="form-label">Proxy API URL (Geonode etc.)</label><input type="url" name="proxy_url" class="form-control" value="{{ config.proxy_url }}"></div>
        <div class="mb-3"><label class="form-label">Zusätzliche manuelle Proxys</label><textarea name="proxies" class="form-control" rows="3">{{ config.proxies }}</textarea></div>
        <button type="submit" class="btn btn-primary">Einstellungen Speichern</button>
    </form></div></div>
</div></body></html>
"""

@app.route("/")
def index(): return render_template_string(HTML_TEMPLATE, config=CONFIG, state=STATE, time=time)

@app.route("/save_settings", methods=["POST"])
def save_settings():
    CONFIG["pushover_user"] = request.form["pushover_user"]
    CONFIG["pushover_token"] = request.form["pushover_token"]
    CONFIG["interval"] = int(request.form["interval"])
    CONFIG["proxies"] = request.form.get("proxies", "")
    CONFIG["proxy_url"] = request.form.get("proxy_url", "")
    CONFIG["require_proxy"] = "require_proxy" in request.form
    save_config()
    return redirect(url_for("index"))

# (Restliche Routes: add_item, delete, start, stop etc. wie gehabt)
@app.route("/add", methods=["POST"])
def add_item():
    CONFIG["items"].append({"id": str(uuid.uuid4())[:8], "name": request.form["name"], "url": request.form["url"], "status": "Wartet...", "has_screenshot": False, "found_time": 0})
    save_config()
    return redirect(url_for("index"))

@app.route("/delete/<item_id>")
def delete_item(item_id):
    CONFIG["items"] = [item for item in CONFIG["items"] if item["id"] != item_id]
    save_config()
    return redirect(url_for("index"))

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
    stop_event.set()
    STATE["is_running"] = False
    return redirect(url_for("index"))

@app.route("/debug/<item_id>")
def serve_debug(item_id):
    path = os.path.join(DATA_DIR, f"debug_{item_id}.png")
    return send_file(path) if os.path.exists(path) else "Kein Bild"

@app.route("/screenshot/<item_id>")
def serve_screenshot(item_id):
    path = os.path.join(DATA_DIR, f"screenshot_{item_id}.png")
    return send_file(path) if os.path.exists(path) else "Kein Bild"

if __name__ == "__main__":
    load_config()
    app.run(host="0.0.0.0", port=5000)
