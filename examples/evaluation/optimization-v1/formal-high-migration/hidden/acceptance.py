import sqlite3,tempfile,subprocess,sys,json,unittest
from pathlib import Path
from migration import migrate
from storage import connect,list_items,add_item
from compat import list_legacy,add_legacy
from legacy import seed
case=unittest.TestCase()

import json
import traceback

def group_0():
    c=sqlite3.connect(':memory:',isolation_level=None)
    migrate(c)
    assert c.execute('PRAGMA user_version').fetchone()[0]==2 and list_items(c)==[]
    assert add_item(c,'A')==1
    migrate(c)
    assert list_items(c)==[{'id':1,'title':'A','done':False}]
    c.close()

def group_1():
    c=sqlite3.connect(':memory:',isolation_level=None)
    seed(c,[(10,'中文','done'),(3,'B','open')]);migrate(c)
    assert list_items(c)==[{'id':3,'title':'B','done':False},{'id':10,'title':'中文','done':True}]
    assert add_item(c,'next')==11
    c.close()

def group_2():
    for rows in [[(1,'A','open'),(2,'B','bad')],[(1,None,'open')]]:
        c=sqlite3.connect(':memory:',isolation_level=None);seed(c,rows)
        with case.assertRaises(ValueError): migrate(c)
        assert c.execute('PRAGMA user_version').fetchone()[0]==1
        assert c.execute('SELECT * FROM items ORDER BY id').fetchall()==rows
        assert [x[1] for x in c.execute('PRAGMA table_info(items)')]==['id','name','status']
        c.close()

def group_3():
    c=sqlite3.connect(':memory:',isolation_level=None);migrate(c);add_legacy(c,'old')
    assert list_legacy(c)==[{'id':1,'name':'old','status':'open'}]
    with case.assertRaises(ValueError): add_item(c,'')
    with case.assertRaises(sqlite3.IntegrityError): c.execute("INSERT INTO items(title,done) VALUES('bad',2)")
    c.execute('PRAGMA user_version=9')
    with case.assertRaises(ValueError): migrate(c)
    c.close()

def group_4():
    with tempfile.TemporaryDirectory() as tmp:
        p=Path(tmp)/'items.db';c=sqlite3.connect(p);seed(c,[(7,'原记录','done')]);c.close()
        r=subprocess.run([sys.executable,'-B','cli.py',str(p),'list'],capture_output=True,text=True,encoding='utf-8')
        assert r.returncode==0 and json.loads(r.stdout)==[{'id':7,'title':'原记录','done':True}]
        r=subprocess.run([sys.executable,'-B','cli.py',str(p),'add','second'],capture_output=True,text=True)
        assert r.returncode==0 and r.stdout.strip()=='8'
        c=connect(p);assert len(list_items(c))==2;c.close()

if __name__ == '__main__':
    failed = 0
    for i in range(5):
        try:
            globals()['group_' + str(i)]()
            result = {'group': i + 1, 'status': 'PASS'}
        except Exception as exc:
            failed += 1
            result = {'group': i + 1, 'status': 'FAIL', 'error': type(exc).__name__}
        print('OMNI_GROUP ' + json.dumps(result), flush=True)
    print('OMNI_ACCEPTANCE_FINISHED', flush=True)
    raise SystemExit(1 if failed else 0)
