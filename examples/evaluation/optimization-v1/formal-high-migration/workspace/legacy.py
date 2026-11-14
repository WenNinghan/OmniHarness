def seed(connection,rows):
    connection.execute('CREATE TABLE items(id INTEGER PRIMARY KEY,name TEXT,status TEXT)')
    connection.executemany('INSERT INTO items VALUES(?,?,?)',rows)
    connection.execute('PRAGMA user_version=1')
    connection.commit()
