from pathlib import Path
def read(name):
    base = Path("/srv/docs").resolve()
    path = (base / name).resolve()
    if not path.is_relative_to(base):
        raise ValueError("outside docs")
    return path.read_text()
