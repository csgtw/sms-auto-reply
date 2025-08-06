import os
import json
from redis import Redis
from logger import log

SERVER = os.getenv("SERVER")
API_KEY = os.getenv("API_KEY")
SECOND_MESSAGE_LINK = os.getenv("SECOND_MESSAGE_LINK")

# Connexion Redis
REDIS_URL = os.getenv("REDIS_URL")
redis_conn = Redis.from_url(REDIS_URL)

def get_conversation_key(number):
    return f"conv:{number}"

def is_archived(number):
    archived = redis_conn.sismember("archived_numbers", number)
    log(f"🧾 Vérification archive [{number}] → {archived}")
    return archived

def archive_number(number):
    redis_conn.sadd("archived_numbers", number)
    log(f"📦 Numéro archivé : {number}")

def mark_message_processed(number, msg_id):
    redis_conn.sadd(f"processed:{number}", msg_id)
    log(f"🧷 Message marqué comme traité : {msg_id} pour {number}")

def is_message_processed(number, msg_id):
    processed = redis_conn.sismember(f"processed:{number}", msg_id)
    log(f"🔍 Vérification message déjà traité [{number}/{msg_id}] → {processed}")
    return processed

def send_request(url, post_data):
    import requests
    log(f"🌐 Requête POST → {url} | data: {post_data}")
    try:
        response = requests.post(url, data=post_data)
        response.raise_for_status()
        data = response.json()
        log(f"📨 Réponse reçue : {data}")
        return data.get("data")
    except Exception as e:
        log(f"❌ Erreur lors de l'envoi POST : {e}")
        return None

def send_single_message(number, message, device_slot):
    log(f"📦 Envoi à {number} via SIM {device_slot} | Msg: {message}")
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
        log(f"❌ Erreur de décodage JSON : {e}")
        return

    number = msg.get("number")
    msg_id = msg.get("ID")
    device_id = msg.get("deviceID")

    msg_id_short = str(msg_id)[-5:] if msg_id else "?????"

    if not number or not msg_id or not device_id:
        log(f"⛔️ [{msg_id_short}] Champs manquants → number={number}, ID={msg_id}, device={device_id}")
        return

    try:
        if is_archived(number):
            log(f"🗃️ [{msg_id_short}] Numéro archivé. Traitement stoppé.")
            return

        if is_message_processed(number, msg_id):
            log(f"🔁 [{msg_id_short}] Message déjà traité. Ignoré.")
            return

        conv_key = get_conversation_key(number)
        step_raw = redis_conn.hget(conv_key, "step")
        step = int(step_raw) if step_raw else 0
        log(f"📊 [{msg_id_short}] Étape actuelle : {step} (raw={step_raw})")

        redis_conn.hset(conv_key, "device", device_id)
        log(f"💾 [{msg_id_short}] Device enregistré : {device_id}")

        if step == 0:
            reply = "C’est le livreur. Votre colis ne rentrait pas dans la boîte aux lettres ce matin. Je repasse ou je le mets en relais ?"
            redis_conn.hset(conv_key, "step", 1)
            log(f"📤 [{msg_id_short}] Réponse étape 0 définie.")
        elif step == 1:
            reply = f"Ok alors choisissez ici votre nouveau créneau ou point relais : {SECOND_MESSAGE_LINK}\nSans ça je peux rien faire, merci et bonne journée."
            redis_conn.hset(conv_key, "step", 2)
            log(f"📤 [{msg_id_short}] Réponse étape 1 définie.")
        else:
            archive_number(number)
            redis_conn.delete(conv_key)
            log(f"✅ [{msg_id_short}] Étapes terminées, conversation archivée et supprimée.")
            return

        result = send_single_message(number, reply, device_id)
        if result:
            log(f"📬 [{msg_id_short}] Message envoyé avec succès.")
        else:
            log(f"⚠️ [{msg_id_short}] Échec d'envoi du message.")

        mark_message_processed(number, msg_id)
        log(f"✅ [{msg_id_short}] Traitement terminé et enregistré.")
    except Exception as e:
        log(f"💥 [{msg_id_short}] Exception non gérée : {e}")
