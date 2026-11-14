import sqlite3
from migration import migrate
def connect(path):
    connection=sqlite3.connect(path,isolation_level=None)
    try: migrate(connection)
    except BaseException: connection.close(); raise
    return connection
def list_items(connection):
    return [{'id':i,'title':title,'done':bool(done)} for i,title,done in connection.execute('SELECT id,title,done FROM items ORDER BY id')]
def add_item(connection,title):
    if not isinstance(title,str) or not title: raise ValueError('title')
    return connection.execute('INSERT INTO items(title,done) VALUES(?,0)',(title,)).lastrowid
