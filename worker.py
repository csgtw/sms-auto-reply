import os
from datetime import datetime
from redis import Redis
from rq import Queue
from rq.serializers import JSONSerializer
from rq.worker import Worker
from logger import log

# ✅ Import process_message avec traçabilité
try:
    from tasks import process_message
    log("📦 Import de process_message depuis tasks.py : OK")
except Exception as e:
    log(f"❌ Échec d'import de tasks.py : {e}")

# ✅ Connexion Redis
REDIS_URL = os.getenv("REDIS_URL")
if not REDIS_URL:
    log("❌ REDIS_URL non défini dans l'environnement")
else:
    log(f"🔌 Connexion Redis à {REDIS_URL}")
redis_conn = Redis.from_url(REDIS_URL)

# ✅ Création file RQ
queue = Queue("default", connection=redis_conn, serializer=JSONSerializer)
log(f"📂 File RQ 'default' prête avec JSONSerializer")

# ✅ Worker personnalisé avec logs extrêmes
class LoggingWorker(Worker):
    def execute_job(self, job, queue):
        log("🛠️ Nouvelle tâche détectée par le worker")
        log(f"🕒 Heure actuelle UTC : {datetime.utcnow().isoformat()}")
        log(f"🆔 ID du job : {job.id}")
        log(f"📄 Description du job : {job.description}")
        log(f"📦 Données internes : {job.to_dict()}")
        try:
            result = super().execute_job(job, queue)
            log(f"✅ Job exécuté avec succès : {job.id}")
            return result
        except Exception as e:
            log(f"💥 Erreur durant l'exécution du job {job.id} : {e}")
            raise

if __name__ == "__main__":
    try:
        log("👷 Worker initialisé, écoute de la file 'default'...")
        worker = LoggingWorker(
            [queue],
            connection=redis_conn,
            serializer=JSONSerializer,
            log_job_description=True
        )
        worker.work(burst=False)
    except Exception as e:
        log(f"🚨 Échec critique au lancement du worker : {e}")
