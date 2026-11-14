def refund(order, balance):
    balance.credit(order.amount)
    order.state = "refunded"
