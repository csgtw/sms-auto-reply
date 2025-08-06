import os
from redis import Redis
from rq import Queue
from rq.serializers import JSONSerializer
from rq.worker import Worker
from logger import log

# ✅ Test d'import de tasks.py
try:
    from tasks import process_message
    log("📦 Import de process_message depuis tasks.py : OK")
except Exception as e:
    log(f"❌ Échec d'import de tasks.py : {e}")

# ❗️ Connexion Redis (sans decode_responses ici)
REDIS_URL = os.getenv("REDIS_URL")
redis_conn = Redis.from_url(REDIS_URL)

# ✅ Queue "default" avec sérialisation JSON
queue = Queue("default", connection=redis_conn, serializer=JSONSerializer)

# ✅ Worker personnalisé avec logs précis
class LoggingWorker(Worker):
    def execute_job(self, job, queue):
        log(f"⚙️ Début traitement du job : {job.id}")
        log(f"📄 Description : {job.description}")
        try:
            result = super().execute_job(job, queue)
            log(f"✅ Job {job.id} terminé avec succès")
            return result
        except Exception as e:
            log(f"💥 Job {job.id} échoué : {e}")
            raise

if __name__ == "__main__":
    try:
        log("👷 Worker lancé, écoute de la file 'default' en cours...")
        worker = LoggingWorker(
            [queue],
            connection=redis_conn,
            serializer=JSONSerializer,
            log_job_description=True
        )
        worker.work(burst=False)  # burst=False = continue à écouter indéfiniment
    except Exception as e:
        log(f"🚨 Erreur critique au lancement du worker : {e}")
