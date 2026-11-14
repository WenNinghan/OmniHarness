from values import checked
DEFAULTS = {'enabled': True, 'retries': 3, 'label': 'local'}
def merge_config(layers):
    result = dict(DEFAULTS)
    for layer in layers: result.update(checked(layer))
    return result
