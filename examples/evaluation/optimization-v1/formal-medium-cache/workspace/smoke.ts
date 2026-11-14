import {TTLCache} from './cache.ts';
const c=new TTLCache<number>(1,()=>0);c.set('a',1,10);if(c.get('a')!==1)throw Error('smoke');
console.log('public smoke passed');
