def get_invoice(db, user, ident):
    return db.invoice(id=ident)
