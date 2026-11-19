from pathlib import Path
import uuid
def upload(name, data):
    if len(data) > 1024:
        raise ValueError("too large")
    (Path("/srv/private-uploads") / uuid.uuid4().hex).write_bytes(data)
