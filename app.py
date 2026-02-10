import os
import json
import hmac
import hashlib
import base64
import uuid
import random
import time

from flask import Flask, request, Response, redirect, url_for, session, render_template_string
from redis import Redis

from logger import log
from tasks import process_message

API_KEY = os.getenv("API_KEY")
DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() == "true"
LOG_FILE = "/tmp/log.txt"

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
APP_SECRET_KEY = os.getenv("APP_SECRET_KEY", "")

SERVER = os.getenv("SERVER")
REDIS_URL = os.getenv("REDIS_URL")
redis_conn = Redis.from_url(REDIS_URL)

CONFIG_KEY = "config:autoreply"

app = Flask(__name__)
app.secret_key = APP_SECRET_KEY or os.urandom(32)


def _is_logged_in():
    return session.get("admin_logged_in") is True


def _require_login():
    if not _is_logged_in():
        return redirect(url_for("admin_login"))
    return None


def _get_config_defaults():
    # ✅ Defaults vides : tout se configure ici
    return {
        "enabled": True,
        "reply_mode": 2,        # 1 ou 2
        "step0_type": "sms",    # sms|mms
        "step1_type": "sms",    # sms|mms
        "step0_text": "",
        "step1_text": "",
    }


def load_config():
    raw = redis_conn.get(CONFIG_KEY)
    defaults = _get_config_defaults()
    if not raw:
        return defaults

    try:
        cfg = json.loads(raw.decode("utf-8"))
        if not isinstance(cfg, dict):
            return defaults

        defaults.update(cfg)

        defaults["enabled"] = bool(defaults.get("enabled", True))
        defaults["reply_mode"] = 1 if int(defaults.get("reply_mode", 2)) == 1 else 2

        if defaults.get("step0_type") not in ("sms", "mms"):
            defaults["step0_type"] = "sms"
        if defaults.get("step1_type") not in ("sms", "mms"):
            defaults["step1_type"] = "sms"

        defaults["step0_text"] = str(defaults.get("step0_text") or "")
        defaults["step1_text"] = str(defaults.get("step1_text") or "")

        return defaults
    except Exception:
        return defaults


def save_config(cfg: dict):
    redis_conn.set(CONFIG_KEY, json.dumps(cfg, ensure_ascii=False))


def _redis_int(key: str) -> int:
    try:
        return int(redis_conn.get(key) or 0)
    except Exception:
        return 0


def _device_stats(device_id: str):
    base = f"stats:device:{device_id}:"
    return {
        "device_id": device_id,
        "received": _redis_int(base + "received"),
        "sent": _redis_int(base + "sent"),
        "errors": _redis_int(base + "errors"),
        "last_seen": _redis_int(base + "last_seen"),
        "cycle": _redis_int(f"cycle:device:{device_id}:index"),
        "cycle_sent": _redis_int(f"cycle:device:{device_id}:sent"),
        "cycle_received": _redis_int(f"cycle:device:{device_id}:received"),
    }


def fetch_gateway_devices():
    """
    ✅ Liste tous les devices du Gateway même si aucun SMS reçu.
    Endpoint confirmé dans ton Gateway : /services/get-devices.php
    """
    import requests

    if not SERVER or not API_KEY:
        return []

    url = f"{SERVER}/services/get-devices.php"
    try:
        r = requests.get(url, params={"key": API_KEY}, timeout=12)
        data = r.json()
        if not data.get("success"):
            return []
        devices = (data.get("data") or {}).get("devices") or []
        return devices
    except Exception as e:
        log(f"❌ fetch_gateway_devices error: {e}")
        return []


# -----------------------
# ADMIN
# -----------------------
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        pwd = (request.form.get("password") or "").strip()
        if ADMIN_PASSWORD and pwd == ADMIN_PASSWORD:
            session["admin_logged_in"] = True
            return redirect(url_for("admin_settings"))
        return Response("Mot de passe incorrect", status=401, mimetype="text/plain")

    return render_template_string("""
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Login</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    :root{--bg:#070a12;--card:#121a2a;--line:#22304a;--txt:#e8eefc;--muted:#8aa0c7;--btn:#2d6cdf;}
    body{margin:0;background:linear-gradient(180deg,#070a12 0%, #0b0f1a 100%);color:var(--txt);font-family:Arial;}
    .wrap{max-width:520px;margin:0 auto;padding:22px;}
    .card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:18px;margin-top:34px;}
    label{display:block;font-size:12px;color:var(--muted);margin-bottom:6px}
    input{
      width:100%;box-sizing:border-box;background:#0e1626;border:1px solid var(--line);
      color:var(--txt);padding:12px;border-radius:12px;outline:none;
    }
    .btn{background:var(--btn);border:0;color:white;padding:12px 14px;border-radius:12px;cursor:pointer;font-weight:800;margin-top:12px;width:100%}
    h2{margin:0 0 8px 0}
    .muted{color:var(--muted);font-size:12px}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h2>Connexion</h2>
      <div class="muted">Accès au panneau de contrôle</div>
      <form method="post" style="margin-top:14px">
        <label>Mot de passe</label>
        <input type="password" name="password" autocomplete="current-password">
        <button class="btn" type="submit">Se connecter</button>
      </form>
    </div>
  </div>
</body>
</html>
""")


@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))


@app.route("/admin", methods=["GET"])
def admin_home():
    guard = _require_login()
    if guard:
        return guard
    return redirect(url_for("admin_settings"))


@app.route("/admin/devices/<device_id>/next_cycle", methods=["POST"])
def admin_next_cycle(device_id):
    guard = _require_login()
    if guard:
        return guard

    device_id = str(device_id)
    new_cycle = redis_conn.incr(f"cycle:device:{device_id}:index")
    redis_conn.set(f"cycle:device:{device_id}:sent", 0)
    redis_conn.set(f"cycle:device:{device_id}:received", 0)

    log(f"🔁 Cycle suivant (manuel) device={device_id} -> cycle={new_cycle}")
    return redirect(url_for("admin_settings"))


@app.route("/admin/settings", methods=["GET", "POST"])
def admin_settings():
    guard = _require_login()
    if guard:
        return guard

    cfg = load_config()

    if request.method == "POST":
        enabled = request.form.get("enabled") == "on"
        reply_mode = int(request.form.get("reply_mode") or 2)

        step0_type = (request.form.get("step0_type") or "sms").strip().lower()
        step1_type = (request.form.get("step1_type") or "sms").strip().lower()

        step0_text = (request.form.get("step0_text") or "").strip()
        step1_text = (request.form.get("step1_text") or "").strip()

        if reply_mode not in (1, 2):
            reply_mode = 2
        if step0_type not in ("sms", "mms"):
            step0_type = "sms"
        if step1_type not in ("sms", "mms"):
            step1_type = "sms"

        # ✅ Si mode 1 réponse : on supprime/ignore Step 1
        if reply_mode == 1:
            step1_text = ""

        cfg.update({
            "enabled": enabled,
            "reply_mode": reply_mode,
            "step0_type": step0_type,
            "step1_type": step1_type,
            "step0_text": step0_text,
            "step1_text": step1_text,
        })
        save_config(cfg)
        return redirect(url_for("admin_settings"))

    # ✅ Devices du gateway (tous) + stats redis
    gw_devices = fetch_gateway_devices()
    rows = []
    for d in gw_devices:
        did = str(d.get("id"))
        s = _device_stats(did)
        s.update({
            "name": d.get("name") or "",
            "model": d.get("model") or "",
            "androidVersion": d.get("androidVersion") or "",
            "appVersion": d.get("appVersion") or "",
            "lastSeenAt": d.get("lastSeenAt") or "",
        })
        rows.append(s)

    now = int(time.time())

    return render_template_string("""
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Control</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    :root{
      --bg:#070a12; --card:#121a2a; --line:#22304a; --txt:#e8eefc; --muted:#8aa0c7;
      --btn:#2d6cdf; --btn2:#0e1626; --good:#24d18f;
    }
    body{margin:0;background:linear-gradient(180deg,#070a12 0%, #0b0f1a 100%);color:var(--txt);font-family:Arial;}
    .wrap{max-width:1200px;margin:0 auto;padding:18px;}
    .top{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;}
    .top h2{margin:0;font-size:18px;}
    a{color:#9bc1ff;text-decoration:none;font-weight:700}
    .grid{display:grid;grid-template-columns:1fr;gap:12px;}
    .card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:16px;}
    .muted{color:var(--muted);font-size:12px}
    .title{font-weight:900;margin-bottom:10px}
    .row{display:flex;gap:12px;flex-wrap:wrap;align-items:flex-end}
    label{display:block;font-size:12px;color:var(--muted);margin-bottom:6px}
    textarea, select{
      width:100%;box-sizing:border-box;background:#0e1626;border:1px solid var(--line);
      color:var(--txt);padding:12px;border-radius:12px;outline:none;
    }
    textarea{min-height:90px;resize:vertical}
    select{appearance:none;background-image:
      linear-gradient(45deg,transparent 50%,#9bc1ff 50%),
      linear-gradient(135deg,#9bc1ff 50%,transparent 50%);
      background-position: calc(100% - 18px) calc(50% - 3px), calc(100% - 12px) calc(50% - 3px);
      background-size: 6px 6px, 6px 6px;
      background-repeat:no-repeat;
    }
    .btn{background:var(--btn);border:0;color:white;padding:10px 14px;border-radius:12px;cursor:pointer;font-weight:900}
    .btn2{background:var(--btn2);border:1px solid var(--line);color:#cfe0ff;padding:9px 12px;border-radius:12px;cursor:pointer;font-weight:900}
    .pill{display:inline-flex;gap:8px;align-items:center;background:#0e1626;border:1px solid var(--line);padding:10px 12px;border-radius:14px}
    .dot{width:9px;height:9px;border-radius:99px;background:var(--good)}
    table{width:100%;border-collapse:collapse}
    th,td{padding:10px;border-bottom:1px solid var(--line);text-align:left;font-size:13px}
    th{color:var(--muted);font-weight:900}
    .hide{display:none}
    code{background:#0e1626;padding:2px 6px;border-radius:10px;border:1px solid var(--line)}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="top">
      <h2>Panneau de contrôle</h2>
      <div style="display:flex;gap:12px;align-items:center">
        <a href="/logs" target="_blank">Logs</a>
        <a href="/admin/logout">Logout</a>
      </div>
    </div>

    <!-- DEVICES en haut -->
    <div class="card">
      <div class="title">Appareils (Gateway)</div>
      <div class="muted" style="margin-bottom:10px">
        Liste complète via <code>/services/get-devices.php</code>. Stats (reçus/envoyés/erreurs) calculées depuis Redis.
      </div>

      <table>
        <thead>
          <tr>
            <th>Device</th>
            <th>Nom / Modèle</th>
            <th>Reçus</th>
            <th>Envoyés</th>
            <th>Erreurs</th>
            <th>Cycle</th>
            <th>Reçus cycle</th>
            <th>Envoyés cycle</th>
            <th>Dernier vu (Gateway)</th>
            <th>Action</th>
          </tr>
        </thead>
        <tbody>
          {% if rows|length == 0 %}
            <tr><td colspan="10" class="muted">Aucun device remonté par le Gateway (vérifie SERVER/API_KEY).</td></tr>
          {% endif %}
          {% for r in rows %}
            <tr>
              <td>
                <div class="pill">
                  <span class="dot"></span>
                  <span style="font-weight:900">#{{ r.device_id }}</span>
                </div>
              </td>
              <td>
                <div style="font-weight:900">{{ r.name }}</div>
                <div class="muted">{{ r.model }} • Android {{ r.androidVersion }} • App {{ r.appVersion }}</div>
              </td>
              <td>{{ r.received }}</td>
              <td>{{ r.sent }}</td>
              <td>{{ r.errors }}</td>
              <td>{{ r.cycle }}</td>
              <td>{{ r.cycle_received }}</td>
              <td>{{ r.cycle_sent }}</td>
              <td class="muted">{{ r.lastSeenAt or "—" }}</td>
              <td>
                <form method="post" action="/admin/devices/{{ r.device_id }}/next_cycle" style="margin:0">
                  <button class="btn2" type="submit">Cycle suivant</button>
                </form>
              </td>
            </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>

    <!-- SETTINGS -->
    <form method="post" class="grid" style="margin-top:12px">

      <div class="card">
        <div class="title">Auto-reply</div>

        <div class="row" style="justify-content:space-between">
          <div class="pill">
            <input id="enabled" type="checkbox" name="enabled" {% if cfg.enabled %}checked{% endif %}>
            <div>
              <div style="font-weight:900">Activé</div>
              <div class="muted">Désactive toutes les réponses</div>
            </div>
          </div>

          <div style="min-width:260px;flex:1;max-width:320px">
            <label>Mode</label>
            <select id="reply_mode" name="reply_mode">
              <option value="1" {% if cfg.reply_mode == 1 %}selected{% endif %}>1 réponse (puis stop)</option>
              <option value="2" {% if cfg.reply_mode == 2 %}selected{% endif %}>2 réponses (puis stop)</option>
            </select>
            <div class="muted">Après la dernière réponse, le numéro est archivé et ne recevra plus rien.</div>
          </div>
        </div>
      </div>

      <div class="card">
        <div class="title">Step 0</div>
        <div class="row">
          <div style="min-width:220px;flex:1;max-width:320px">
            <label>Type</label>
            <select name="step0_type">
              <option value="sms" {% if cfg.step0_type == 'sms' %}selected{% endif %}>sms</option>
              <option value="mms" {% if cfg.step0_type == 'mms' %}selected{% endif %}>mms</option>
            </select>
          </div>
        </div>
        <div style="margin-top:10px">
          <label>Message</label>
          <textarea name="step0_text">{{ cfg.step0_text }}</textarea>
        </div>
      </div>

      <div id="step1_block" class="card">
        <div class="title">Step 1</div>
        <div class="row">
          <div style="min-width:220px;flex:1;max-width:320px">
            <label>Type</label>
            <select name="step1_type">
              <option value="sms" {% if cfg.step1_type == 'sms' %}selected{% endif %}>sms</option>
              <option value="mms" {% if cfg.step1_type == 'mms' %}selected{% endif %}>mms</option>
            </select>
          </div>
        </div>
        <div style="margin-top:10px">
          <label>Message</label>
          <textarea name="step1_text">{{ cfg.step1_text }}</textarea>
          <div class="muted">Mets le lien directement dans le texte.</div>
        </div>
      </div>

      <div class="card">
        <button class="btn" type="submit">Sauvegarder</button>
        <div class="muted" style="margin-top:10px">
          Webhook inchangé : <code>/sms_auto_reply</code>
        </div>
      </div>
    </form>
  </div>

  <script>
    function toggleStep1() {
      const mode = document.getElementById("reply_mode").value;
      const block = document.getElementById("step1_block");
      if (mode === "1") block.classList.add("hide");
      else block.classList.remove("hide");
    }
    document.getElementById("reply_mode").addEventListener("change", toggleStep1);
    toggleStep1();
  </script>
</body>
</html>
""", cfg=cfg, rows=rows, now=now)


# -----------------------
# WEBHOOK
# -----------------------
@app.route("/sms_auto_reply", methods=["POST"])
def sms_auto_reply():
    request_id = str(uuid.uuid4())[:8]
    log(f"\n📩 [{request_id}] Nouvelle requête POST reçue")

    messages_raw = request.form.get("messages")
    if not messages_raw:
        log(f"[{request_id}] ❌ Champ 'messages' manquant")
        return "messages manquants", 400

    log(f"[{request_id}] 🔎 messages brut : {messages_raw}")

    if not DEBUG_MODE:
        signature = request.headers.get("X-SG-SIGNATURE")
        if not signature:
            log(f"[{request_id}] ❌ Signature manquante")
            return "Signature requise", 403

        expected_hash = base64.b64encode(
            hmac.new(API_KEY.encode(), messages_raw.encode(), hashlib.sha256).digest()
        ).decode()

        if signature != expected_hash:
            log(f"[{request_id}] ❌ Signature invalide")
            return "Signature invalide", 403

        log(f"[{request_id}] ✅ Signature valide")

    try:
        messages = json.loads(messages_raw)
        log(f"[{request_id}] ✔️ messages parsés : {messages}")
    except json.JSONDecodeError as e:
        log(f"[{request_id}] ❌ JSON invalide : {e}")
        return "Format JSON invalide", 400

    if not isinstance(messages, list):
        return "Liste attendue", 400

    for i, msg in enumerate(messages):
        try:
            delay = random.randint(60, 180)
            log(f"[{request_id}] ⏱️ Mise en file message {i} avec délai {delay}s")
            result = process_message.apply_async(args=[json.dumps(msg)], countdown=delay)
            log(f"[{request_id}] ✅ Celery ID : {result.id}")
        except Exception as e:
            log(f"[{request_id}] ❌ Erreur Celery : {e}")

    return "OK", 200


@app.route("/logs")
def logs():
    if not os.path.exists(LOG_FILE):
        return Response("Aucun log", mimetype="text/plain")
    with open(LOG_FILE, "r", encoding="utf-8") as f:
        return Response(f.read(), mimetype="text/plain")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
