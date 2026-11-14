def refund(order, balance):
    with order.lock():
        if order.state != "paid":
            raise ValueError("invalid transition")
        balance.credit(order.amount)
        order.state = "refunded"
