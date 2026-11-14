from values import checked
DEFAULTS = {'enabled': True, 'retries': 3, 'label': 'local'}
def merge_config(layers):
    result = DEFAULTS
    for layer in reversed(layers): result.update({k:v for k,v in layer.items() if v})
    return result
