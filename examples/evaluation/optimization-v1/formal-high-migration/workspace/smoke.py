import sqlite3
from migration import migrate
c=sqlite3.connect(':memory:',isolation_level=None);migrate(c)
assert c.execute('PRAGMA user_version').fetchone()[0]==2
print('public smoke passed')
