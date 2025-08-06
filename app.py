import os
import json
import hmac
import hashlib
import base64
import uuid
import random
from datetime import timedelta
from flask import Flask, request, Response
from redis import Redis
from rq import Queue
from rq.serializers import JSONSerializer
from tasks import process_message
from logger import log

API_KEY = os.getenv("API_KEY")
DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() == "true"
LOG_FILE = "/tmp/log.txt"

app = Flask(__name__)

# Connexion Redis
REDIS_URL = os.getenv("REDIS_URL")
log(f"🔌 REDIS_URL = {REDIS_URL}")
try:
    redis_conn = Redis.from_url(REDIS_URL)
    redis_conn.ping()
    log("✅ Connexion Redis réussie")
except Exception as e:
    log(f"❌ Connexion Redis échouée : {e}")
    raise

# Queue RQ "default"
try:
    queue = Queue("default", connection=redis_conn, serializer=JSONSerializer)
    log("✅ Queue RQ 'default' initialisée avec JSONSerializer")
except Exception as e:
    log(f"❌ Erreur d'initialisation de la queue RQ : {e}")
    raise

@app.route('/sms_auto_reply', methods=['POST'])
def sms_auto_reply():
    request_id = str(uuid.uuid4())[:8]
    log(f"\n📩 [{request_id}] Nouvelle requête POST reçue")

    try:
        messages_raw = request.form.get("messages")
        if not messages_raw:
            log(f"[{request_id}] ❌ Champ 'messages' manquant")
            return "messages manquants", 400
        log(f"[{request_id}] 🔎 messages brut : {messages_raw}")
    except Exception as e:
        log(f"[{request_id}] ❌ Erreur lecture messages : {e}")
        return "Erreur lecture", 400

    # Vérification de signature (si pas en DEBUG)
    if not DEBUG_MODE:
        try:
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
        except Exception as e:
            log(f"[{request_id}] ❌ Erreur lors de la vérification de signature : {e}")
            return "Erreur signature", 400

    # Parsing JSON
    try:
        messages = json.loads(messages_raw)
        log(f"[{request_id}] ✔️ messages parsés : {messages}")
    except json.JSONDecodeError as e:
        log(f"[{request_id}] ❌ JSON invalide : {e}")
        return "Format JSON invalide", 400

    if not isinstance(messages, list):
        log(f"[{request_id}] ❌ Format JSON non liste (type: {type(messages)})")
        return "Liste attendue", 400

    # Mise en file avec délai aléatoire entre 1 et 3 minutes
    for i, msg in enumerate(messages):
        try:
            delay = random.randint(60, 180)
            log(f"[{request_id}] ⏱️ Préparation mise en file message {i} avec délai {delay}s : {msg}")
            job = queue.enqueue_in(timedelta(seconds=delay), process_message, json.dumps(msg))
            log(f"[{request_id}] ✅ Job {i} en file avec ID {job.id}, exécution prévue à {job.enqueued_at + timedelta(seconds=delay)}")
        except Exception as e:
            log(f"[{request_id}] ❌ Erreur mise en file message {i} : {e}")

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
