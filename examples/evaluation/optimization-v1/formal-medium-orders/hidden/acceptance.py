from aggregate import summarize
import unittest,subprocess,sys,tempfile,json
from pathlib import Path
case=unittest.TestCase()

import json
import traceback

def group_0():
    assert summarize('id,customer,amount\n')==[]
    assert summarize('id,customer,amount\n1,A,1.20\n2,A,0.09\n')==[{'customer':'A','cents':129}]

def group_1():
    assert summarize('id,customer,amount\n1,"张,三",0.29\n2,"多\n行",1\n')==[{'customer':'多\n行','cents':100},{'customer':'张,三','cents':29}]

def group_2():
    for text in ['id,customer,amount\n1,A,1junk\n','id,customer,amount\n1,A,1\n1,B,2\n','id,customer,amount\n1,A,-1\n','id,customer,amount\n1,A,1.001\n','id,customer,amount\n1,A\n','id,customer,amount\n1,,1\n']:
        with case.assertRaises(ValueError): summarize(text)

def group_3():
    assert summarize('id,customer,amount\n1,Z,0.29\n2,A,1\n3,Z,0.29\n')==[{'customer':'A','cents':100},{'customer':'Z','cents':58}]

def group_4():
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp);src=root/'input.csv';out=root/'out.json'
        src.write_text('id,customer,amount\n1,甲,1\n',encoding='utf-8')
        r=subprocess.run([sys.executable,'-B','cli.py',str(src),str(out)],capture_output=True,text=True)
        assert r.returncode==0 and json.loads(out.read_text(encoding='utf-8'))==[{'customer':'甲','cents':100}]
        out.write_text('KEEP',encoding='utf-8');src.write_text('invalid',encoding='utf-8')
        r=subprocess.run([sys.executable,'-B','cli.py',str(src),str(out)],capture_output=True,text=True)
        assert r.returncode==2 and out.read_text()=='KEEP' and r.stderr.strip()=='INVALID_ORDERS'

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
