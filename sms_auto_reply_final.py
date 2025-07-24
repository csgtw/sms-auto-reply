import json
import os
import hmac
import hashlib
import base64
from flask import Flask, request, abort
from datetime import datetime

# Configuration
SERVER = "https://coursier-prbs.com/"
API_KEY = "39a97416a08e10e381674867f42cf3a3d1f98bf1"
STORAGE_FILE = os.path.join(os.path.dirname(__file__), 'conversations.json')
LOG_FILE = os.path.join(os.path.dirname(__file__), 'log.txt')
DEBUG_MODE = True  # Désactiver la vérification de signature si besoin

app = Flask(__name__)

def send_request(url, post_data):
    import requests
    response = requests.post(url, data=post_data)
    try:
        json_data = response.json()
    except ValueError:
        raise Exception("Réponse invalide du serveur.")
    if not json_data.get("success"):
        error = json_data.get("error", {}).get("message", "Erreur inconnue")
        raise Exception(f"Erreur d'envoi : {error}")
    return json_data.get("data")

def send_single_message(number, message, device_slot):
    post_data = {
        'number': number,
        'message': message,
        'devices': device_slot,
        'type': 'mms',
        'prioritize': 'true',
        'key': API_KEY,
    }
    return send_request(f"{SERVER}/services/send.php", post_data)

def load_json(path):
    if not os.path.exists(path):
        return {}
    with open(path, 'r', encoding='utf-8') as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}

def save_json(path, data):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def log(text):
    with open(LOG_FILE, 'a', encoding='utf-8') as f:
        f.write(f"[{datetime.utcnow().isoformat()}] {text}\n")

@app.route('/sms_auto_reply', methods=['POST'])
def sms_auto_reply():
    conversations = load_json(STORAGE_FILE)

    if request.method != 'POST':
        abort(405)

    messages_raw = request.form.get("messages")
    if not messages_raw:
        log("❌ messages_raw manquant")
        return "Requête invalide : messages manquants", 400

    if not DEBUG_MODE and "X-SG-SIGNATURE" in request.headers:
        signature = request.headers.get("X-SG-SIGNATURE")
        expected_hash = base64.b64encode(hmac.new(API_KEY.encode(), messages_raw.encode(), hashlib.sha256).digest()).decode()
        if signature != expected_hash:
            log("❌ Signature invalide")
            return "Signature invalide", 403

    try:
        messages = json.loads(messages_raw)
        log(f"✔️ messages_raw reçu : {messages_raw}")
    except json.JSONDecodeError:
        log("❌ Format JSON invalide")
        return "Format JSON invalide", 400

    for msg in messages:
        msg_id = msg.get("ID")
        number = msg.get("number")
        device_from_msg = msg.get("device")

        if not msg_id or not number or not device_from_msg:
            log(f"⛔️ Message ignoré : ID={msg_id}, number={number}, device={device_from_msg}")
            continue

        if number not in conversations:
            conversations[number] = {
                "step": 0,
                "device": device_from_msg,
                "processed_ids": []
            }
            log(f"🆕 Nouvelle conversation avec {number}")

        if msg_id in conversations[number]["processed_ids"]:
            log(f"🔁 Message déjà traité (ID={msg_id}) pour {number}")
            continue

        step = conversations[number]["step"]
        device_id = conversations[number]["device"]

        if step == 0:
            reply = "C’est le livreur. Votre colis ne rentrait pas dans la boîte aux lettres ce matin. Je repasse ou je le mets en relais ?"
            conversations[number]["step"] = 1
        elif step == 1:
            reply = "Ok alors choisissez ici votre nouveau créneau ou point relais : https://suivi-coursier.com/183248\nSans ça je peux rien faire, merci et bonne journée."
            conversations[number]["step"] = 2
        else:
            log(f"✅ Fin de conversation avec {number}")
            conversations.pop(number, None)
            continue

        try:
            send_single_message(number, reply, device_id)
            log(f"📤 Envoi à {number} via {device_id} : {reply}")
        except Exception as e:
            log(f"❌ Erreur envoi à {number} : {str(e)}")

        conversations[number]["processed_ids"].append(msg_id)
        conversations[number]["processed_ids"] = list(set(conversations[number]["processed_ids"]))[-10:]

    save_json(STORAGE_FILE, conversations)
    log(f"💾 Conversations sauvegardées")
    return "✔️ Messages traités avec succès", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
