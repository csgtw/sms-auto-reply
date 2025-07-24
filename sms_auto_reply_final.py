import json
import os
import hmac
import hashlib
import base64
from flask import Flask, request, abort, Response
from datetime import datetime

# Configuration
SERVER = "https://moncolis-attente.com/"
API_KEY = "f376d32d14b058ed2383b97fd568d1b26de1b75c"
STORAGE_FILE = os.path.join(os.path.dirname(__file__), 'conversations.json')
LOG_FILE = '/tmp/log.txt'  # ✅ Compatible avec Render
DEBUG_MODE = True  # Pour ignorer la signature pendant les tests

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
    log("📩 Nouvelle requête POST reçue")

    conversations = load_json(STORAGE_FILE)

    messages_raw = request.form.get("messages")
    if not messages_raw:
        log("❌ messages_raw manquant")
        return "Requête invalide : messages manquants", 400

    log(f"🔎 messages_raw brut : {messages_raw}")

    if not DEBUG_MODE and "X-SG-SIGNATURE" in request.headers:
        signature = request.headers.get("X-SG-SIGNATURE")
        expected_hash = base64.b64encode(hmac.new(API_KEY.encode(), messages_raw.encode(), hashlib.sha256).digest()).decode()
        if signature != expected_hash:
            log("❌ Signature invalide")
            return "Signature invalide", 403

    try:
        messages = json.loads(messages_raw)
        log(f"✔️ JSON messages parsé : {messages}")
    except json.JSONDecodeError:
        log("❌ Format JSON invalide")
        return "Format JSON invalide", 400

    for msg in messages:
        msg_id = msg.get("ID")
        number = msg.get("number")
        device_from_msg = msg.get("deviceID")  # ✅ CORRECTION

        log(f"🔁 Nouveau message détecté : ID={msg_id}, number={number}, device={device_from_msg}")

        if not msg_id or not number or not device_from_msg:
            log("⛔️ Champs manquants, message ignoré")
            continue

        if number not in conversations:
            conversations[number] = {
                "step": 0,
                "device": device_from_msg,
                "processed_ids": []
            }
            log(f"🆕 Création conversation pour {number}")

        if msg_id in conversations[number]["processed_ids"]:
            log(f"🔁 Message déjà traité pour {number}, ID={msg_id}")
            continue

        step = conversations[number]["step"]
        device_id = conversations[number]["device"]
        log(f"➡️ Étape actuelle : {step} | Device : {device_id}")

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
            log(f"📤 Message envoyé à {number} : {reply}")
        except Exception as e:
            log(f"❌ Erreur lors de l’envoi à {number} : {str(e)}")

        conversations[number]["processed_ids"].append(msg_id)
        conversations[number]["processed_ids"] = list(set(conversations[number]["processed_ids"]))[-10:]

    save_json(STORAGE_FILE, conversations)
    log("💾 Conversations sauvegardées ✅")
    return "✔️ Messages traités avec succès", 200

@app.route('/logs', methods=['GET'])
def read_logs():
    if not os.path.exists(LOG_FILE):
        return Response("Aucun log trouvé", mimetype='text/plain')
    with open(LOG_FILE, 'r', encoding='utf-8') as f:
        content = f.read()
    return Response(content, mimetype='text/plain')

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
