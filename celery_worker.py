import os
from celery import Celery
from logger import log

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# 🔒 Gérer SSL si rediss:// est utilisé
ssl_options = {}
if REDIS_URL.startswith("rediss://"):
    ssl_options = {
        "ssl_cert_reqs": "none"  # Pour Upstash, aucun certificat requis
    }

# ✅ Initialisation de Celery
celery = Celery(
    "sms_auto_reply",
    broker=REDIS_URL,
    backend=REDIS_URL,
    include=["tasks"]
)

celery.conf.update(
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    broker_use_ssl=ssl_options if REDIS_URL.startswith("rediss://") else None,
    redis_backend_use_ssl=ssl_options if REDIS_URL.startswith("rediss://") else None,
)

# ✅ Log au démarrage
try:
    log("✅ Celery initialisé avec succès (broker & backend Redis)")
except Exception as e:
    print(f"❌ Erreur init Celery : {e}")
