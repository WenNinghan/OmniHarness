import json,sys
from storage import connect,list_items,add_item
def main(argv):
    if len(argv)<2: return 2
    try:
        connection=connect(argv[0])
        try:
            if argv[1]=='list' and len(argv)==2: print(json.dumps(list_items(connection),ensure_ascii=False,sort_keys=True))
            elif argv[1]=='add' and len(argv)==3: print(add_item(connection,argv[2]))
            else: return 2
        finally: connection.close()
    except (ValueError,Exception) as exc:
        print('INVALID_STORE',file=sys.stderr);return 2
    return 0
if __name__=='__main__': raise SystemExit(main(sys.argv[1:]))
