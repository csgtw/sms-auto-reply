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
from celery_worker import celery
from tasks import process_message


API_KEY = os.getenv("API_KEY")
DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() == "true"
LOG_FILE = "/tmp/log.txt"

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
APP_SECRET_KEY = os.getenv("APP_SECRET_KEY", "")

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
    # ✅ defaults vides : tout se configure via l’UI
    return {
        "enabled": True,
        "reply_mode": 2,
        "min_in_before_reply": 1,
        "step0_type": "sms",
        "step1_type": "sms",
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

        # normalisation
        defaults["enabled"] = bool(defaults.get("enabled", True))
        defaults["reply_mode"] = 1 if int(defaults.get("reply_mode", 2)) == 1 else 2
        defaults["min_in_before_reply"] = max(1, int(defaults.get("min_in_before_reply", 1)))
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
    :root { --bg:#0b0f1a; --card:#121a2a; --muted:#8aa0c7; --txt:#e8eefc; --line:#22304a; --btn:#2d6cdf; }
    body{margin:0;background:linear-gradient(180deg,#070a12 0%, #0b0f1a 100%);color:var(--txt);font-family:Arial;}
    .wrap{max-width:520px;margin:0 auto;padding:22px;}
    .card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;margin-top:30px;}
    label{display:block;font-size:12px;color:var(--muted);margin-bottom:6px}
    input{
      width:100%;box-sizing:border-box;background:#0e1626;border:1px solid var(--line);
      color:var(--txt);padding:10px;border-radius:10px;outline:none;
    }
    .btn{background:var(--btn);border:0;color:white;padding:10px 14px;border-radius:10px;cursor:pointer;font-weight:700;margin-top:12px;width:100%}
    h2{margin:0 0 10px 0}
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


@app.route("/admin")
def admin_home():
    guard = _require_login()
    if guard:
        return guard
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
        min_in_before_reply = int(request.form.get("min_in_before_reply") or 1)

        step0_type = (request.form.get("step0_type") or "sms").strip().lower()
        step1_type = (request.form.get("step1_type") or "sms").strip().lower()

        step0_text = (request.form.get("step0_text") or "").strip()
        step1_text = (request.form.get("step1_text") or "").strip()

        if reply_mode not in (1, 2):
            reply_mode = 2
        if min_in_before_reply < 1:
            min_in_before_reply = 1
        if step0_type not in ("sms", "mms"):
            step0_type = "sms"
        if step1_type not in ("sms", "mms"):
            step1_type = "sms"

        # ✅ ergonomie : si mode 1 réponse, on vide step1
        if reply_mode == 1:
            step1_text = ""

        cfg.update({
            "enabled": enabled,
            "reply_mode": reply_mode,
            "min_in_before_reply": min_in_before_reply,
            "step0_type": step0_type,
            "step1_type": step1_type,
            "step0_text": step0_text,
            "step1_text": step1_text,
        })
        save_config(cfg)
        return redirect(url_for("admin_settings"))

    return render_template_string("""
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Settings</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    :root { --bg:#0b0f1a; --card:#121a2a; --muted:#8aa0c7; --txt:#e8eefc; --line:#22304a; --btn:#2d6cdf; --bad:#ff5c7a; }
    body{margin:0;background:linear-gradient(180deg,#070a12 0%, #0b0f1a 100%);color:var(--txt);font-family:Arial;}
    .wrap{max-width:980px;margin:0 auto;padding:22px;}
    .top{display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;}
    .top h2{margin:0;font-size:20px;}
    a{color:#9bc1ff;text-decoration:none}
    .nav{display:flex;gap:12px;align-items:center}
    .grid{display:grid;grid-template-columns:1fr;gap:12px;}
    .card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;}
    .row{display:flex;gap:12px;flex-wrap:wrap}
    label{display:block;font-size:12px;color:var(--muted);margin-bottom:6px}
    input[type="number"], select, textarea{
      width:100%;box-sizing:border-box;background:#0e1626;border:1px solid var(--line);
      color:var(--txt);padding:10px;border-radius:10px;outline:none;
    }
    textarea{min-height:92px;resize:vertical}
    .btn{background:var(--btn);border:0;color:white;padding:10px 14px;border-radius:10px;cursor:pointer;font-weight:700}
    .pill{display:inline-flex;align-items:center;gap:10px;background:#0e1626;border:1px solid var(--line);padding:10px 12px;border-radius:12px}
    .muted{color:var(--muted);font-size:12px}
    .sep{height:1px;background:var(--line);margin:14px 0}
    .hide{display:none}
    .title{font-weight:700;margin-bottom:10px}
    code{background:#0e1626;padding:2px 6px;border-radius:8px;border:1px solid var(--line)}
  </style>
</head>

<body>
  <div class="wrap">
    <div class="top">
      <h2>Auto-reply • Settings</h2>
      <div class="nav">
        <a href="/admin/devices">Devices</a>
        <a href="/admin/logout">Logout</a>
      </div>
    </div>

    <form method="post" class="grid">

      <div class="card">
        <div class="row" style="align-items:center;justify-content:space-between">
          <div class="pill">
            <input id="enabled" type="checkbox" name="enabled" {% if cfg.enabled %}checked{% endif %}>
            <div>
              <div style="font-weight:700">Activé</div>
              <div class="muted">Désactive toutes les réponses</div>
            </div>
          </div>

          <div style="min-width:260px">
            <label>Mode</label>
            <select id="reply_mode" name="reply_mode">
              <option value="1" {% if cfg.reply_mode == 1 %}selected{% endif %}>1 réponse</option>
              <option value="2" {% if cfg.reply_mode == 2 %}selected{% endif %}>2 réponses</option>
            </select>
          </div>

          <div style="min-width:300px">
            <label>Répondre après N messages entrants (par numéro)</label>
            <input type="number" min="1" name="min_in_before_reply" value="{{ cfg.min_in_before_reply }}">
            <div class="muted">Ex: 2 = pas de réponse au 1er message, réponse à partir du 2e</div>
          </div>
        </div>
      </div>

      <div class="card">
        <div class="title">Step 0</div>
        <div class="row">
          <div style="flex:1;min-width:240px">
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
          <div style="flex:1;min-width:240px">
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
""", cfg=cfg)


def _device_stats(device_id: str):
    base = f"stats:device:{device_id}:"
    received = int(redis_conn.get(base + "received") or 0)
    sent = int(redis_conn.get(base + "sent") or 0)
    errors = int(redis_conn.get(base + "errors") or 0)
    last_seen = int(redis_conn.get(base + "last_seen") or 0)

    cycle = int(redis_conn.get(f"cycle:device:{device_id}:index") or 0)
    cycle_sent = int(redis_conn.get(f"cycle:device:{device_id}:sent") or 0)
    cycle_received = int(redis_conn.get(f"cycle:device:{device_id}:received") or 0)

    return {
        "device_id": device_id,
        "received": received,
        "sent": sent,
        "errors": errors,
        "last_seen": last_seen,
        "cycle": cycle,
        "cycle_sent": cycle_sent,
        "cycle_received": cycle_received,
    }


@app.route("/admin/devices", methods=["GET"])
def admin_devices():
    guard = _require_login()
    if guard:
        return guard

    # devices connus : on les apprend en recevant des webhooks
    device_ids = []
    try:
        raw = redis_conn.smembers("devices:seen")
        device_ids = sorted([x.decode("utf-8") for x in raw])
    except Exception:
        device_ids = []

    rows = [_device_stats(d) for d in device_ids]

    now = int(time.time())

    return render_template_string("""
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Devices</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    :root { --bg:#0b0f1a; --card:#121a2a; --muted:#8aa0c7; --txt:#e8eefc; --line:#22304a; --btn:#2d6cdf; --btn2:#1f2b44; }
    body{margin:0;background:linear-gradient(180deg,#070a12 0%, #0b0f1a 100%);color:var(--txt);font-family:Arial;}
    .wrap{max-width:1100px;margin:0 auto;padding:22px;}
    .top{display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;}
    .top h2{margin:0;font-size:20px;}
    a{color:#9bc1ff;text-decoration:none}
    .nav{display:flex;gap:12px;align-items:center}
    .card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;}
    table{width:100%;border-collapse:collapse}
    th,td{padding:10px;border-bottom:1px solid var(--line);text-align:left;font-size:13px}
    th{color:var(--muted);font-weight:700}
    .muted{color:var(--muted);font-size:12px}
    .btn{background:var(--btn);border:0;color:white;padding:8px 10px;border-radius:10px;cursor:pointer;font-weight:700}
    .btn2{background:var(--btn2);border:1px solid var(--line);color:#cfe0ff;padding:8px 10px;border-radius:10px;cursor:pointer;font-weight:700}
    .pill{display:inline-block;padding:4px 8px;border-radius:999px;background:#0e1626;border:1px solid var(--line);color:#cfe0ff;font-size:12px}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="top">
      <h2>Devices</h2>
      <div class="nav">
        <a href="/admin/settings">Settings</a>
        <a href="/admin/logout">Logout</a>
      </div>
    </div>

    <div class="card">
      <div class="muted" style="margin-bottom:10px">
        Liste basée sur les devices vus via webhooks (devices:seen). Le bouton “Cycle suivant” est manuel.
      </div>

      <table>
        <thead>
          <tr>
            <th>Device</th>
            <th>Reçus</th>
            <th>Envoyés</th>
            <th>Erreurs</th>
            <th>Cycle</th>
            <th>Reçus cycle</th>
            <th>Envoyés cycle</th>
            <th>Dernier vu</th>
            <th>Action</th>
          </tr>
        </thead>
        <tbody>
          {% if rows|length == 0 %}
            <tr><td colspan="9" class="muted">Aucun device vu pour le moment. Dès qu’un SMS arrive, il apparaîtra ici.</td></tr>
          {% endif %}

          {% for r in rows %}
            <tr>
              <td><span class="pill">{{ r.device_id }}</span></td>
              <td>{{ r.received }}</td>
              <td>{{ r.sent }}</td>
              <td>{{ r.errors }}</td>
              <td>{{ r.cycle }}</td>
              <td>{{ r.cycle_received }}</td>
              <td>{{ r.cycle_sent }}</td>
              <td class="muted">
                {% if r.last_seen == 0 %}
                  —
                {% else %}
                  {{ (now - r.last_seen) }}s
                {% endif %}
              </td>
              <td>
                <form method="post" action="/admin/devices/{{ r.device_id }}/next_cycle" style="margin:0">
                  <button class="btn" type="submit">Cycle suivant</button>
                </form>
              </td>
            </tr>
          {% endfor %}
        </tbody>
      </table>

    </div>
  </div>
</body>
</html>
""", rows=rows, now=now)


@app.route("/admin/devices/<device_id>/next_cycle", methods=["POST"])
def admin_next_cycle(device_id):
    guard = _require_login()
    if guard:
        return guard

    # ✅ incrémente cycle manuellement (pas d’envoi automatique ici)
    redis_conn.sadd("devices:seen", device_id)

    new_cycle = redis_conn.incr(f"cycle:device:{device_id}:index")
    redis_conn.set(f"cycle:device:{device_id}:sent", 0)
    redis_conn.set(f"cycle:device:{device_id}:received", 0)

    log(f"🔁 Cycle suivant (manuel) device={device_id} -> cycle={new_cycle}")
    return redirect(url_for("admin_devices"))


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

    # ✅ Signature
    if not DEBUG_MODE:
        signature = request.headers.get("X-SG-SIGNATURE")
        if not signature:
            log(f"[{request_id}] ❌ Signature manquante")
            return "Signature requise", 403

        expected_hash = base64.b64encode(
            hmac.new(API_KEY.encode(), messages_raw.encode(), hashlib.sha256).digest()
        ).decode()

        if signature != expected_hash:
            log(f"[{request_id}] ❌ Signature invalide (reçue: {signature})")
            return "Signature invalide", 403

        log(f"[{request_id}] ✅ Signature valide")

    # ✅ Parsing JSON
    try:
        messages = json.loads(messages_raw)
        log(f"[{request_id}] ✔️ messages parsés : {messages}")
    except json.JSONDecodeError as e:
        log(f"[{request_id}] ❌ JSON invalide : {e}")
        return "Format JSON invalide", 400

    if not isinstance(messages, list):
        log(f"[{request_id}] ❌ Format JSON non liste")
        return "Liste attendue", 400

    # ✅ Mise en file Celery (60 à 180 sec)
    for i, msg in enumerate(messages):
        try:
            delay = random.randint(60, 180)
            log(f"[{request_id}] ⏱️ Mise en file message {i} avec délai {delay}s")
            result = process_message.apply_async(args=[json.dumps(msg)], countdown=delay)
            log(f"[{request_id}] ✅ Job {i} Celery ID : {result.id}")
        except Exception as e:
            log(f"[{request_id}] ❌ Erreur Celery file {i} : {e}")

    log(f"[{request_id}] 🏁 Tous les messages sont en file")
    return "OK", 200


@app.route("/logs")
def logs():
    if not os.path.exists(LOG_FILE):
        return Response("Aucun log", mimetype="text/plain")
    with open(LOG_FILE, "r", encoding="utf-8") as f:
        return Response(f.read(), mimetype="text/plain")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
