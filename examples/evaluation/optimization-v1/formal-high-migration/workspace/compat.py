from storage import list_items,add_item
def list_legacy(connection):return list_items(connection)
def add_legacy(connection,name):return add_item(connection,name)
