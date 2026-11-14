from candidate import parse_duration
import unittest
case=unittest.TestCase()

import json
import traceback

def group_0():
    assert parse_duration('1s') == 1000 and parse_duration('250ms') == 250

def group_1():
    assert parse_duration(' 00.50s \n') == 500 and parse_duration('0ms') == 0

def group_2():
    for text in ['1msjunk','1.2.3s','1 s','.5s','1.s','1e3ms','+1s','-1s','1']:
        with case.assertRaises(ValueError): parse_duration(text)

def group_3():
    for text in ['NaNs','Infinityms', '9'*400+'s', '']:
        with case.assertRaises(ValueError): parse_duration(text)

def group_4():
    for item in [None,1,False,[],{}]:
        with case.assertRaises(TypeError): parse_duration(item)
    assert abs(parse_duration('0.125ms') - 0.125) < 1e-12

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
