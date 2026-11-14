from importer import apply_batch
import tempfile,json,unittest,subprocess,sys
from pathlib import Path
case=unittest.TestCase()

import json
import traceback

def group_0():
    with tempfile.TemporaryDirectory() as tmp:
        p=Path(tmp)/'state';r=apply_batch(p,[{'id':'A','value':1}],0)
        assert r=={'records':{'A':1},'offset':1} and json.loads(p.read_text())==r

def group_1():
    with tempfile.TemporaryDirectory() as tmp:
        p=Path(tmp)/'state';apply_batch(p,[{'id':'A','value':1}],0);before=p.read_bytes()
        assert apply_batch(p,[{'id':'A','value':1}],0)['offset']==1 and p.read_bytes()==before
        assert apply_batch(p,[{'id':'A','value':1},{'id':'B','value':2}],1)['offset']==3

def group_2():
    with tempfile.TemporaryDirectory() as tmp:
        p=Path(tmp)/'state';apply_batch(p,[{'id':'A','value':1}],0);before=p.read_bytes()
        for rows,offset in [([{'id':'B','value':2},{'id':'A','value':9}],1),([{'id':'B','value':True}],1),([],3)]:
            with case.assertRaises(ValueError):apply_batch(p,rows,offset)
            assert p.read_bytes()==before

def group_3():
    with tempfile.TemporaryDirectory() as tmp:
        p=Path(tmp)/'state'
        for offset in [-1,True,1.2]:
            with case.assertRaises(ValueError):apply_batch(p,[],offset)
        with case.assertRaises(ValueError):apply_batch(p,[{'id':'','value':1}],0)
        assert not p.exists()

def group_4():
    with tempfile.TemporaryDirectory() as tmp:
        p=Path(tmp)/'state';r=subprocess.run([sys.executable,'-B','cli.py',str(p),'0','[{"id":"A","value":1}]'],capture_output=True,text=True)
        assert r.returncode==0 and json.loads(r.stdout)['offset']==1
        r=subprocess.run([sys.executable,'-B','cli.py',str(p),'3','[]'],capture_output=True,text=True)
        assert r.returncode==2 and r.stderr.strip()=='INVALID_BATCH'

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
