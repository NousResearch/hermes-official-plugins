def fetch(client, key):
    urls = {"help": "https://help.example.invalid/manual"}
    return client.get(urls[key], allow_redirects=False)
