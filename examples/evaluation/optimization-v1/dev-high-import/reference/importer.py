from copy import deepcopy
from validate import validated
from state import load,save
def apply_batch(path,rows,offset):
    if type(offset) is not int or offset<0:raise ValueError('offset')
    batch=validated(rows);old=load(path)
    if offset<old['offset']:
        if offset+len(batch)<=old['offset']:return old
        raise ValueError('overlap')
    if offset!=old['offset']:raise ValueError('gap')
    new=deepcopy(old)
    for r in batch:
        if r['id'] in new['records'] and new['records'][r['id']]!=r['value']:raise ValueError('conflict')
        new['records'][r['id']]=r['value']
    new['offset']+=len(batch);save(path,new);return new
