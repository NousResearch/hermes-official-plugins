import secrets
def token():
    return secrets.token_urlsafe(32)
