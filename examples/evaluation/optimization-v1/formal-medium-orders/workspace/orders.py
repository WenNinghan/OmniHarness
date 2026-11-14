def read_orders(text):
    rows=[]
    for line in text.splitlines()[1:]:
        identity,customer,amount=line.split(',')
        rows.append({'id':identity,'customer':customer,'cents':int(float(amount)*100)})
    return rows
