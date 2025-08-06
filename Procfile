web: gunicorn app:app
worker: python worker.py
scheduler: echo $REDIS_URL && rqscheduler --url $REDIS_URL
