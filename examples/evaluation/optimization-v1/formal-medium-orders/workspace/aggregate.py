from orders import read_orders
def summarize(text):
    totals={}
    for row in read_orders(text): totals[row['customer']]=row['cents']
    return [{'customer':key,'cents':value} for key,value in totals.items()]
