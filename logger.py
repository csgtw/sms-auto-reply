import os
from datetime import datetime, timezone

LOG_FILE = "/tmp/log.txt"

def log(text):
    now = datetime.now(timezone.utc).isoformat()
    prefix = f"[{os.getenv('REQUEST_ID', 'worker')}]"  # Permet d’identifier d’où ça vient
    line = f"{now} {prefix} {text}\n"
    print(line.strip())  # Toujours visible dans console
    with open(LOG_FILE, 'a', encoding='utf-8') as f:
        f.write(line)
        f.flush()  # ✅ Forcer écriture immédiate
