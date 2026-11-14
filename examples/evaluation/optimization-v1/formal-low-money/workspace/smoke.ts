import {sumCents} from './candidate.ts';
if(sumCents(['1','2'])!==300) throw new Error('smoke');
console.log('public smoke passed');
