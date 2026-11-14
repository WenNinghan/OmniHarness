from candidate import merge_intervals

import json
import traceback

def group_0():
    assert merge_intervals([]) == []

def group_1():
    assert merge_intervals([[3,4],[1,2]]) == [[1,2],[3,4]]

def group_2():
    assert merge_intervals([[1,8],[2,3],[8,9]]) == [[1,9]]

def group_3():
    data=[[1,4],[2,3]]
    assert merge_intervals(data)==[[1,4]]
    assert data==[[1,4],[2,3]]

def group_4():
    assert merge_intervals([[0,0],[0,1],[-2,-1]]) == [[-2,-1],[0,1]]

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
