import os
from celery import Celery
from logger import log



REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# Initialisation de Celery
celery = Celery(
    "sms_auto_reply",
    broker=REDIS_URL,
    backend=REDIS_URL,
    include=["tasks"]  # importe tasks.py pour enregistrer les tâches
)

# Configuration
celery.conf.update(
    timezone="UTC",
    task_track_started=True,
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    enable_utc=True
)

# Log pour vérification
try:
    log("✅ Celery initialisé avec succès (broker & backend Redis)")
except Exception as e:
    print(f"❌ Erreur init Celery : {e}")
