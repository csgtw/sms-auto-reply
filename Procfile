web: gunicorn app:app
worker: python worker.py
scheduler: rqscheduler --url $REDIS_URL
