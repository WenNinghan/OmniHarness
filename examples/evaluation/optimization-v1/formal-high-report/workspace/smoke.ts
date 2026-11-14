import {report} from './pipeline.ts';
if(!report('category,cents\nA,1\n','csv').includes('A,1,1'))throw Error('smoke');
console.log('public smoke passed');
