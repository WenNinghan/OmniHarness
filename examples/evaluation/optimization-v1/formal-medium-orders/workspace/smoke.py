from aggregate import summarize
assert summarize('id,customer,amount\n1,A,1\n')==[{'customer':'A','cents':100}]
print('public smoke passed')
