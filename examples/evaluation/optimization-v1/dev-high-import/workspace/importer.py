from state import load,save
def apply_batch(path,rows,offset):
    state=load(path)
    for row in rows:
        state['records'][row['id']]=row['value'];state['offset']+=1;save(path,state)
    return state
