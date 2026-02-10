import os
import json
import time
from redis import Redis
from logger import log
from celery_worker import celery  # 🔁 Import du Celery app

SERVER = os.getenv("SERVER")
API_KEY = os.getenv("API_KEY")

# ✅ Connexion Redis
REDIS_URL = os.getenv("REDIS_URL")
redis_conn = Redis.from_url(REDIS_URL)

CONFIG_KEY = "config:autoreply"


def _config_defaults():
    # ✅ Defaults vides : tout est réglé depuis /admin/settings
    return {
        "enabled": True,
        "reply_mode": 2,
        "min_in_before_reply": 1,
        "step0_type": "sms",   # sms|mms
        "step1_type": "sms",   # sms|mms
        "step0_text": "",
        "step1_text": "",
    }


def load_config():
    raw = redis_conn.get(CONFIG_KEY)
    defaults = _config_defaults()
    if not raw:
        return defaults

    try:
        cfg = json.loads(raw.decode("utf-8"))
        if not isinstance(cfg, dict):
            return defaults

        defaults.update(cfg)

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


def _stat_incr(device_id: str, key: str, amount: int = 1):
    redis_conn.incrby(f"stats:device:{device_id}:{key}", amount)


def _stat_last_seen(device_id: str):
    redis_conn.set(f"stats:device:{device_id}:last_seen", int(time.time()))


def _cycle_incr_received(device_id: str, amount: int = 1):
    redis_conn.incrby(f"cycle:device:{device_id}:received", amount)


def _cycle_incr_sent(device_id: str, amount: int = 1):
    redis_conn.incrby(f"cycle:device:{device_id}:sent", amount)


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


def send_single_message(number, message, device_slot, msg_type):
    # ✅ sécurité : si message vide → ne rien envoyer
    if not (message or "").strip():
        log(f"⛔️ Message vide → aucun envoi vers {number} (type={msg_type})")
        return None

    log(f"📦 Envoi à {number} via device {device_slot} (type={msg_type})")
    return send_request(f"{SERVER}/services/send.php", {
        "number": number,
        "message": message,
        "devices": device_slot,
        "type": msg_type,     # sms|mms
        "prioritize": 1,
        "key": API_KEY,
    })


@celery.task(name="process_message")
def process_message(msg_json):
    log("🔧 Début de process_message")
    log(f"🛎️ Job brut reçu : {msg_json}")

    cfg = load_config()
    if not cfg.get("enabled", True):
        log("⏸️ Auto-reply désactivé (config:autoreply.enabled=false).")
        return

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

    # ✅ enregistre le device comme “vu”
    try:
        redis_conn.sadd("devices:seen", str(device_id))
        _stat_last_seen(str(device_id))
        _stat_incr(str(device_id), "received", 1)
        _cycle_incr_received(str(device_id), 1)
    except Exception:
        pass

    try:
        if is_archived(number):
            log(f"🗃️ [{msg_id_short}] Numéro archivé, ignoré.")
            return

        if is_message_processed(number, msg_id):
            log(f"🔁 [{msg_id_short}] Message déjà traité, ignoré.")
            return

        conv_key = get_conversation_key(number)

        # ✅ Compteur entrants par NUMERO (conversation)
        in_count = redis_conn.hincrby(conv_key, "in_count", 1)
        min_in = int(cfg.get("min_in_before_reply", 1))
        log(f"📥 [{msg_id_short}] in_count={in_count} (min_in_before_reply={min_in})")

        if in_count < min_in:
            mark_message_processed(number, msg_id)
            log(f"⏳ [{msg_id_short}] Pas de réponse (seuil non atteint).")
            return

        step = int(redis_conn.hget(conv_key, "step") or 0)
        redis_conn.hset(conv_key, "device", device_id)

        log(f"📊 [{msg_id_short}] Étape actuelle : {step}")

        reply_mode = int(cfg.get("reply_mode", 2))
        step0_text = cfg.get("step0_text") or ""
        step1_text = cfg.get("step1_text") or ""
        step0_type = cfg.get("step0_type", "sms")
        step1_type = cfg.get("step1_type", "sms")

        if step == 0:
            reply = step0_text
            redis_conn.hset(conv_key, "step", 1)
            msg_type = step0_type
            log(f"📤 [{msg_id_short}] Step 0 prêt.")
        elif step == 1:
            if reply_mode == 1:
                archive_number(number)
                redis_conn.delete(conv_key)
                mark_message_processed(number, msg_id)
                log(f"✅ [{msg_id_short}] Mode 1 réponse: conversation archivée (pas de step1).")
                return

            reply = step1_text
            redis_conn.hset(conv_key, "step", 2)
            msg_type = step1_type
            log(f"📤 [{msg_id_short}] Step 1 prêt.")
        else:
            archive_number(number)
            redis_conn.delete(conv_key)
            log(f"✅ [{msg_id_short}] Conversation terminée et archivée.")
            return

        send_single_message(number, reply, device_id, msg_type)

        # ✅ stats envoi (si message non vide)
        if (reply or "").strip():
            try:
                _stat_incr(str(device_id), "sent", 1)
                _cycle_incr_sent(str(device_id), 1)
            except Exception:
                pass

        mark_message_processed(number, msg_id)
        log(f"✅ [{msg_id_short}] Traitement terminé (envoi tenté si message non vide).")
        log(f"🏁 [{msg_id_short}] Fin du traitement")

    except Exception as e:
        log(f"💥 [{msg_id_short}] Erreur interne : {e}")
        try:
            _stat_incr(str(device_id), "errors", 1)
        except Exception:
            pass
