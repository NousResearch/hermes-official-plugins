from pathlib import Path
def upload(name, data):
    (Path("/srv/www") / name).write_bytes(data)
