def validated(rows):
    output=[]
    for r in rows:
        if not isinstance(r,dict) or set(r)!={'id','value'} or type(r['id']) is not str or not r['id'] or type(r['value']) is not int: raise ValueError('row')
        output.append(dict(r))
    return output
