from config import merge_config, DEFAULTS
import unittest,json,subprocess,sys
case=unittest.TestCase()

import json
import traceback

def group_0():
    assert merge_config([])=={'enabled':True,'retries':3,'label':'local'}

def group_1():
    assert merge_config([{'retries':1},{'retries':2}])['retries']==2
    assert merge_config([{'enabled':False,'retries':0,'label':''}])=={'enabled':False,'retries':0,'label':''}

def group_2():
    for values in [{'bad':1},{'enabled':1},{'retries':True},{'retries':10},{'label':None}]:
        with case.assertRaises(ValueError): merge_config([values])

def group_3():
    x={'retries':1}
    merge_config([x])
    assert x=={'retries':1} and DEFAULTS['retries']==3
    assert merge_config([])['retries']==3

def group_4():
    r=subprocess.run([sys.executable,'-B','cli.py','{"enabled":false}'],capture_output=True,text=True)
    assert r.returncode==0 and json.loads(r.stdout)['enabled'] is False
    r=subprocess.run([sys.executable,'-B','cli.py','{"oops":1}'],capture_output=True,text=True)
    assert r.returncode==2 and not r.stdout and r.stderr.strip()=='INVALID_CONFIG'

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
