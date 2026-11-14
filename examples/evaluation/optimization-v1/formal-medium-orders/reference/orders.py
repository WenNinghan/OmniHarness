import csv, io, re
def read_orders(text):
    reader = csv.DictReader(io.StringIO(text, newline=''))
    if reader.fieldnames != ['id','customer','amount']: raise ValueError('header')
    result,seen=[],set()
    for row in reader:
        if None in row or any(value is None for value in row.values()): raise ValueError('columns')
        identity,customer,amount=row['id'],row['customer'],row['amount']
        if not identity or not customer or not re.fullmatch(r'(?:0|[1-9][0-9]*)(?:\.[0-9]{1,2})?',amount): raise ValueError('row')
        if identity in seen: raise ValueError('duplicate id')
        seen.add(identity)
        whole,_,part=amount.partition('.')
        result.append({'id':identity,'customer':customer,'cents':int(whole)*100+int(part.ljust(2,'0') or '0')})
    return result
