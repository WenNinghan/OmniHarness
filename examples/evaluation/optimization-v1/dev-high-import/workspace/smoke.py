import tempfile
from pathlib import Path
from importer import apply_batch
with tempfile.TemporaryDirectory() as d:
    assert apply_batch(Path(d)/'state',[{'id':'A','value':1}],0)['offset']==1
print('public smoke passed')
