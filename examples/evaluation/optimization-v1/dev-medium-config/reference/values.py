def checked(values):
    result = {}
    for key,value in values.items():
        if key not in {'enabled','retries','label'}: raise ValueError('unknown key')
        if key == 'enabled' and type(value) is not bool: raise ValueError('enabled')
        if key == 'retries' and (type(value) is not int or not 0 <= value <= 9): raise ValueError('retries')
        if key == 'label' and (type(value) is not str or len(value) > 30): raise ValueError('label')
        result[key] = value
    return result
