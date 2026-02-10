import os
import json
import time
from redis import Redis
from logger import log
from celery_worker import celery

SERVER = os.getenv("SERVER")
API_KEY = os.getenv("API_KEY")

REDIS_URL = os.getenv("REDIS_URL")
redis_conn = Redis.from_url(REDIS_URL)

CONFIG_KEY = "config:autoreply"


def _config_defaults():
    return {
        "enabled": True,
        "reply_mode": 2,
        "step0_type": "sms",
        "step1_type": "sms",
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
    log(f"🌐 POST → {url} | data: {post_data}")
    try:
        response = requests.post(url, data=post_data)
        data = response.json()
        log(f"📨 Réponse : {data}")
        return data.get("data")
    except Exception as e:
        log(f"❌ Erreur POST : {e}")
        return None


def send_single_message(number, message, device_slot, msg_type):
    if not (message or "").strip():
        log(f"⛔️ Message vide → aucun envoi vers {number} (type={msg_type})")
        return None

    log(f"📦 Envoi à {number} via device {device_slot} (type={msg_type})")
    return send_request(f"{SERVER}/services/send.php", {
        "number": number,
        "message": message,
        "devices": device_slot,
        "type": msg_type,
        "prioritize": 1,
        "key": API_KEY,
    })


@celery.task(name="process_message")
def process_message(msg_json):
    log("🔧 Début process_message")
    log(f"🛎️ Job brut : {msg_json}")

    cfg = load_config()
    if not cfg.get("enabled", True):
        log("⏸️ Auto-reply désactivé.")
        return

    try:
        msg = json.loads(msg_json)
    except Exception as e:
        log(f"❌ JSON invalide : {e}")
        return

    number = msg.get("number")
    msg_id = msg.get("ID")
    device_id = msg.get("deviceID")

    msg_id_short = str(msg_id)[-5:] if msg_id else "?????"

    if not number or not msg_id or not device_id:
        log(f"⛔️ [{msg_id_short}] Champs manquants")
        return

    device_id = str(device_id)

    # ✅ Stats device (reçus)
    try:
        _stat_last_seen(device_id)
        _stat_incr(device_id, "received", 1)
        _cycle_incr_received(device_id, 1)
    except Exception:
        pass

    try:
        if is_archived(number):
            log(f"🗃️ [{msg_id_short}] Numéro archivé → ignoré.")
            return

        if is_message_processed(number, msg_id):
            log(f"🔁 [{msg_id_short}] Déjà traité → ignoré.")
            return

        conv_key = get_conversation_key(number)
        step = int(redis_conn.hget(conv_key, "step") or 0)
        redis_conn.hset(conv_key, "device", device_id)

        reply_mode = int(cfg.get("reply_mode", 2))
        step0_text = cfg.get("step0_text") or ""
        step1_text = cfg.get("step1_text") or ""
        step0_type = cfg.get("step0_type", "sms")
        step1_type = cfg.get("step1_type", "sms")

        # ✅ IMPORTANT : après le message final → archive immédiatement
        if step == 0:
            reply = step0_text
            msg_type = step0_type

            send_single_message(number, reply, device_id, msg_type)

            # stats envoyés
            if (reply or "").strip():
                try:
                    _stat_incr(device_id, "sent", 1)
                    _cycle_incr_sent(device_id, 1)
                except Exception:
                    pass

            mark_message_processed(number, msg_id)

            if reply_mode == 1:
                # 1 réponse => stop direct
                archive_number(number)
                redis_conn.delete(conv_key)
                log(f"✅ [{msg_id_short}] Mode 1 réponse → archivé après Step0.")
                return

            # mode 2 => on attend step1 au prochain message entrant
            redis_conn.hset(conv_key, "step", 1)
            log(f"✅ [{msg_id_short}] Step0 envoyé, attente Step1.")
            return

        if step == 1:
            if reply_mode == 1:
                # sécurité : si repasse en mode1, on stop
                archive_number(number)
                redis_conn.delete(conv_key)
                mark_message_processed(number, msg_id)
                log(f"✅ [{msg_id_short}] Mode 1 → stop.")
                return

            reply = step1_text
            msg_type = step1_type

            send_single_message(number, reply, device_id, msg_type)

            if (reply or "").strip():
                try:
                    _stat_incr(device_id, "sent", 1)
                    _cycle_incr_sent(device_id, 1)
                except Exception:
                    pass

            mark_message_processed(number, msg_id)

            # ✅ Step final => archive direct
            archive_number(number)
            redis_conn.delete(conv_key)
            log(f"✅ [{msg_id_short}] Step1 envoyé → archivé, stop total.")
            return

        # Tout le reste => stop
        archive_number(number)
        redis_conn.delete(conv_key)
        log(f"✅ [{msg_id_short}] Step inconnu/terminé → archivé.")
        return

    except Exception as e:
        log(f"💥 [{msg_id_short}] Erreur interne : {e}")
        try:
            _stat_incr(device_id, "errors", 1)
        except Exception:
            pass
