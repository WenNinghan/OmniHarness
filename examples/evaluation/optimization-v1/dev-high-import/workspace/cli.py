import json,sys
from importer import apply_batch
def main(args):
    try:
        if len(args)!=3:raise ValueError('args')
        print(json.dumps(apply_batch(args[0],json.loads(args[2]),int(args[1])),sort_keys=True));return 0
    except (ValueError,TypeError):print('INVALID_BATCH',file=sys.stderr);return 2
if __name__=='__main__':raise SystemExit(main(sys.argv[1:]))
