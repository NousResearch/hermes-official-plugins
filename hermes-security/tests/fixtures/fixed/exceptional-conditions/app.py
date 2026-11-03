def allowed(check):
    try:
        return check()
    except Exception:
        return False
