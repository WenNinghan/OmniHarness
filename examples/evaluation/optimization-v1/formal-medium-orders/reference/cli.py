import json, os, sys, tempfile
from pathlib import Path
from aggregate import summarize
def main(argv):
    if len(argv)!=2: return 2
    source,target=map(Path,argv)
    try:
        data=summarize(source.read_text(encoding='utf-8'))
        serialized=json.dumps(data,ensure_ascii=False,sort_keys=True)+'\n'
    except (ValueError, OSError):
        print('INVALID_ORDERS',file=sys.stderr)
        return 2
    descriptor,temp=tempfile.mkstemp(dir=target.parent,prefix='.orders-')
    try:
        with os.fdopen(descriptor,'w',encoding='utf-8') as stream: stream.write(serialized)
        os.replace(temp,target)
    finally:
        if os.path.exists(temp): os.unlink(temp)
    return 0
if __name__=='__main__': raise SystemExit(main(sys.argv[1:]))
