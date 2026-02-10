import os
import json
import hmac
import hashlib
import base64
import uuid
import random
from flask import Flask, request, Response, redirect, url_for, session, render_template_string
from redis import Redis
from tasks import process_message
from logger import log
from celery_worker import celery  # 🔄 nouvelle import

API_KEY = os.getenv("API_KEY")
DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() == "true"
LOG_FILE = "/tmp/log.txt"

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
APP_SECRET_KEY = os.getenv("APP_SECRET_KEY", "")

app = Flask(__name__)
app.secret_key = APP_SECRET_KEY or os.urandom(32)

# ✅ Connexion Redis
REDIS_URL = os.getenv("REDIS_URL")
redis_conn = Redis.from_url(REDIS_URL)

CONFIG_KEY = "config:autoreply"

def _is_logged_in():
    return session.get("admin_logged_in") is True

def _require_login():
    if not _is_logged_in():
        return redirect(url_for("admin_login"))
    return None

def _get_config_defaults():
    # Defaults = ton comportement actuel (2 réponses, envoi mms)
    return {
        "enabled": True,
        "reply_mode": 2,               # 1 ou 2
        "min_in_before_reply": 1,      # répondre dès le 1er message
        "step0_type": "mms",           # sms|mms
        "step1_type": "mms",           # sms|mms
        "step0_text": "C’est le livreur. Votre colis ne rentrait pas dans la boîte aux lettres ce matin. Je repasse ou je le mets en relais ?",
        "step1_text": "Ok alors choisissez ici votre nouveau créneau ou point relais : {link}\nSans ça je peux rien faire, merci et bonne journée.",
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
        # merge
        defaults.update(cfg)
        return defaults
    except Exception:
        return defaults

def save_config(cfg: dict):
    redis_conn.set(CONFIG_KEY, json.dumps(cfg, ensure_ascii=False))

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
<html><head><meta charset="utf-8"><title>Login</title></head>
<body style="font-family:Arial;padding:24px;max-width:520px;margin:auto;">
  <h2>Connexion</h2>
  <form method="post">
    <label>Mot de passe</label><br>
    <input type="password" name="password" style="width:100%;padding:10px;margin:10px 0;">
    <button type="submit" style="padding:10px 14px;">Se connecter</button>
  </form>
</body></html>
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

        step0_type = (request.form.get("step0_type") or "mms").strip().lower()
        step1_type = (request.form.get("step1_type") or "mms").strip().lower()

        step0_text = (request.form.get("step0_text") or "").strip()
        step1_text = (request.form.get("step1_text") or "").strip()

        # Validation minimaliste
        if reply_mode not in (1, 2):
            reply_mode = 2
        if min_in_before_reply < 1:
            min_in_before_reply = 1
        if step0_type not in ("sms", "mms"):
            step0_type = "mms"
        if step1_type not in ("sms", "mms"):
            step1_type = "mms"

        cfg.update({
            "enabled": enabled,
            "reply_mode": reply_mode,
            "min_in_before_reply": min_in_before_reply,
            "step0_type": step0_type,
            "step1_type": step1_type,
            "step0_text": step0_text or cfg.get("step0_text", ""),
            "step1_text": step1_text or cfg.get("step1_text", ""),
        })
        save_config(cfg)
        return redirect(url_for("admin_settings"))

    return render_template_string("""
<!doctype html>
<html><head><meta charset="utf-8"><title>Settings</title></head>
<body style="font-family:Arial;padding:24px;max-width:900px;margin:auto;">
  <div style="display:flex;justify-content:space-between;align-items:center;">
    <h2>Auto-reply settings</h2>
    <a href="/admin/logout">Logout</a>
  </div>

  <form method="post" style="border:1px solid #ddd;padding:16px;border-radius:10px;">
    <label><input type="checkbox" name="enabled" {% if cfg.enabled %}checked{% endif %}> Activé</label>
    <hr>

    <label>Mode</label><br>
    <select name="reply_mode" style="padding:8px;width:200px;">
      <option value="1" {% if cfg.reply_mode == 1 %}selected{% endif %}>1 réponse</option>
      <option value="2" {% if cfg.reply_mode == 2 %}selected{% endif %}>2 réponses</option>
    </select>

    <div style="margin-top:12px;">
      <label>Répondre seulement après N messages entrants (min 1)</label><br>
      <input name="min_in_before_reply" value="{{ cfg.min_in_before_reply }}" style="padding:8px;width:120px;">
    </div>

    <hr>

    <div style="display:flex;gap:30px;flex-wrap:wrap;">
      <div>
        <label>Step 0 type</label><br>
        <select name="step0_type" style="padding:8px;width:200px;">
          <option value="sms" {% if cfg.step0_type == 'sms' %}selected{% endif %}>sms</option>
          <option value="mms" {% if cfg.step0_type == 'mms' %}selected{% endif %}>mms</option>
        </select>
      </div>
      <div>
        <label>Step 1 type</label><br>
        <select name="step1_type" style="padding:8px;width:200px;">
          <option value="sms" {% if cfg.step1_type == 'sms' %}selected{% endif %}>sms</option>
          <option value="mms" {% if cfg.step1_type == 'mms' %}selected{% endif %}>mms</option>
        </select>
      </div>
    </div>

    <hr>

    <label>Texte Step 0</label><br>
    <textarea name="step0_text" rows="3" style="width:100%;padding:10px;">{{ cfg.step0_text }}</textarea>

    <div style="margin-top:12px;">
      <label>Texte Step 1 (utilise {link})</label><br>
      <textarea name="step1_text" rows="3" style="width:100%;padding:10px;">{{ cfg.step1_text }}</textarea>
    </div>

    <div style="margin-top:14px;">
      <button type="submit" style="padding:10px 14px;">Sauvegarder</button>
    </div>
  </form>

  <p style="margin-top:14px;color:#666;">
    Webhook inchangé : <code>/sms_auto_reply</code> — tes services Render restent identiques.
  </p>
</body></html>
""", cfg=cfg)

@app.route('/sms_auto_reply', methods=['POST'])
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

    # ✅ Mise en file Celery avec délai aléatoire (60 à 180 sec)
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

@app.route('/logs')
def logs():
    if not os.path.exists(LOG_FILE):
        return Response("Aucun log", mimetype='text/plain')
    with open(LOG_FILE, 'r', encoding='utf-8') as f:
        return Response(f.read(), mimetype='text/plain')

if __name__ == '__main__':
    app.run(host="0.0.0.0", port=5000)
