import json,os,tempfile
from pathlib import Path
def load(path):
    p=Path(path)
    return json.loads(p.read_text(encoding='utf-8')) if p.exists() else {'records':{},'offset':0}
def save(path,state):
    p=Path(path);fd,name=tempfile.mkstemp(dir=p.parent,prefix='.state-')
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as stream:json.dump(state,stream,sort_keys=True)
        os.replace(name,p)
    finally:
        if os.path.exists(name):os.unlink(name)
