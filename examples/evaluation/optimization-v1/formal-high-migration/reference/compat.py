from storage import list_items,add_item
def list_legacy(connection):
    return [{'id':r['id'],'name':r['title'],'status':'done' if r['done'] else 'open'} for r in list_items(connection)]
def add_legacy(connection,name): return add_item(connection,name)
