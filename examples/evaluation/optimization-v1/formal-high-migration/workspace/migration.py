from schema import CREATE_V2
def migrate(connection):
    version=connection.execute('PRAGMA user_version').fetchone()[0]
    if version==2:return
    rows=[]
    if version==1:
        rows=connection.execute('SELECT id,name,status FROM items').fetchall()
        connection.execute('DROP TABLE items')
    connection.execute(CREATE_V2)
    for i,name,status in rows:connection.execute('INSERT INTO items(title,done) VALUES(?,?)',(name,status=='done'))
    connection.execute('PRAGMA user_version=2')
