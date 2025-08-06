import os
import json
import hmac
import hashlib
import base64
import uuid
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

# ✅ Connexion Redis
REDIS_URL = os.getenv("REDIS_URL")
redis_conn = Redis.from_url(REDIS_URL)

# ✅ Queue RQ nommée "default"
queue = Queue("default", connection=redis_conn, serializer=JSONSerializer)

@app.route('/sms_auto_reply', methods=['POST'])
def sms_auto_reply():
    request_id = str(uuid.uuid4())[:8]  # pour suivre les logs
    log(f"\n📩 [{request_id}] Nouvelle requête POST reçue")

    messages_raw = request.form.get("messages")
    if not messages_raw:
        log(f"[{request_id}] ❌ Champ 'messages' manquant")
        return "messages manquants", 400

    log(f"[{request_id}] 🔎 messages brut : {messages_raw}")

    # ✅ Vérification de signature si non en DEBUG
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

    # ✅ Mise en file
    for i, msg in enumerate(messages):
        try:
            job = queue.enqueue(process_message, json.dumps(msg))
            log(f"[{request_id}] ➡️ Mise en file {i} : {msg} ✅ job.id: {job.id}")
        except Exception as e:
            log(f"[{request_id}] ❌ Erreur file {i} : {e}")

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
