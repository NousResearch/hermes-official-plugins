def lookup(db, name):
    return db.execute("SELECT * FROM users WHERE name = ?", (name,))
