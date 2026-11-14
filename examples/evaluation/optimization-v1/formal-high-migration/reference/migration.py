from schema import CREATE_V2
def migrate(connection):
    version=connection.execute('PRAGMA user_version').fetchone()[0]
    if version==2: return
    if version not in (0,1): raise ValueError('unsupported schema')
    connection.execute('BEGIN IMMEDIATE')
    try:
        if version==1:
            rows=connection.execute('SELECT id,name,status FROM items ORDER BY id').fetchall()
            if any(status not in ('open','done') or name is None for _,name,status in rows): raise ValueError('legacy data')
            connection.execute('ALTER TABLE items RENAME TO items_old')
            connection.execute(CREATE_V2)
            connection.executemany('INSERT INTO items VALUES(?,?,?)',[(i,n,1 if s=='done' else 0) for i,n,s in rows])
            connection.execute('DROP TABLE items_old')
        else: connection.execute(CREATE_V2)
        connection.execute('PRAGMA user_version=2')
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
