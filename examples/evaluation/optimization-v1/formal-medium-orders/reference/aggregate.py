from orders import read_orders
def summarize(text):
    totals={}
    for row in read_orders(text): totals[row['customer']]=totals.get(row['customer'],0)+row['cents']
    return [{'customer':key,'cents':totals[key]} for key in sorted(totals)]
