import os
from celery import Celery
from dotenv import load_dotenv

# 🔁 Pour charger les variables d’environnement sur Render ou en local (optionnel)
load_dotenv()

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

celery = Celery(
    "sms_auto_reply",
    broker=REDIS_URL,
    backend=REDIS_URL,
    include=["tasks"]
)

# Configuration optionnelle
celery.conf.update(
    timezone='UTC',
    task_track_started=True,
    task_serializer='json',
    accept_content=['json'],
    result_serializer='json',
)
