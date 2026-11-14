from config import merge_config
assert merge_config([{'retries':2}])['retries']==2
print('public smoke passed')
