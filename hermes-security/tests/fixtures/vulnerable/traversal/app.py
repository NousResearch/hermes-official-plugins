from pathlib import Path
def read(name):
    return (Path("/srv/docs") / name).read_text()
