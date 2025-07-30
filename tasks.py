import os
import json
from redis import Redis
from logger import log

SERVER = os.getenv("SERVER")
API_KEY = os.getenv("API_KEY")
SECOND_MESSAGE_LINK = os.getenv("SECOND_MESSAGE_LINK")

# ✅ Connexion Redis (sans decode_responses ici)
REDIS_URL = os.getenv("REDIS_URL")
redis_conn = Redis.from_url(REDIS_URL)

def get_conversation_key(number):
    return f"conv:{number}"

def is_archived(number):
    return redis_conn.sismember("archived_numbers", number)

def archive_number(number):
    redis_conn.sadd("archived_numbers", number)

def mark_message_processed(number, msg_id):
    redis_conn.sadd(f"processed:{number}", msg_id)

def is_message_processed(number, msg_id):
    return redis_conn.sismember(f"processed:{number}", msg_id)

def send_request(url, post_data):
    import requests
    log(f"🌐 Requête POST → {url} | data: {post_data}")
    try:
        response = requests.post(url, data=post_data)
        data = response.json()
        log(f"📨 Réponse reçue : {data}")
        return data.get("data")
    except Exception as e:
        log(f"❌ Erreur POST : {e}")
        return None

def send_single_message(number, message, device_slot):
    log(f"📦 Envoi à {number} via SIM {device_slot}")
    return send_request(f"{SERVER}/services/send.php", {
        'number': number,
        'message': message,
        'devices': device_slot,
        'type': 'mms',
        'prioritize': 1,
        'key': API_KEY,
    })

def process_message(msg_json):
    log("🔧 Début de process_message")
    log(f"🛎️ Job brut reçu : {msg_json}")

    try:
        msg = json.loads(msg_json)
        log(f"🧩 JSON décodé : {msg}")
    except Exception as e:
        log(f"❌ Erreur JSON : {e}")
        return

    number = msg.get("number")
    msg_id = msg.get("ID")
    device_id = msg.get("deviceID")

    msg_id_short = str(msg_id)[-5:] if msg_id else "?????"

    if not number or not msg_id or not device_id:
        log(f"⛔️ [{msg_id_short}] Champs manquants : number={number}, ID={msg_id}, device={device_id}")
        return

    try:
        if is_archived(number):
            log(f"🗃️ [{msg_id_short}] Numéro archivé, ignoré.")
            return
        if is_message_processed(number, msg_id):
            log(f"🔁 [{msg_id_short}] Message déjà traité, ignoré.")
            return

        conv_key = get_conversation_key(number)
        step = int(redis_conn.hget(conv_key, "step") or 0)
        redis_conn.hset(conv_key, "device", device_id)

        log(f"📊 [{msg_id_short}] Étape actuelle : {step}")

        if step == 0:
            reply = "C’est le livreur. Votre colis ne rentrait pas dans la boîte aux lettres ce matin. Je repasse ou je le mets en relais ?"
            redis_conn.hset(conv_key, "step", 1)
            log(f"📤 [{msg_id_short}] Réponse étape 0 envoyée.")
        elif step == 1:
            reply = f"Ok alors choisissez ici votre nouveau créneau ou point relais : {SECOND_MESSAGE_LINK}\nSans ça je peux rien faire, merci et bonne journée."
            redis_conn.hset(conv_key, "step", 2)
            log(f"📤 [{msg_id_short}] Réponse étape 1 envoyée.")
        else:
            archive_number(number)
            redis_conn.delete(conv_key)
            log(f"✅ [{msg_id_short}] Conversation terminée et archivée.")
            return

        send_single_message(number, reply, device_id)
        mark_message_processed(number, msg_id)
        log(f"✅ [{msg_id_short}] Réponse envoyée : {reply}")
        log(f"🏁 [{msg_id_short}] Fin du traitement de ce message")

    except Exception as e:
        log(f"💥 [{msg_id_short}] Erreur interne : {e}")
