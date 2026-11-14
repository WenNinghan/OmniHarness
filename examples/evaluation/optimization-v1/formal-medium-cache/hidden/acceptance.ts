import assert from 'node:assert/strict';
import {TTLCache} from './cache.ts';
const groups: Array<() => void> = [
() => {
let now=0;const c=new TTLCache<number>(2,()=>now);c.set('a',1,10);assert.equal(c.get('a'),1);now=10;assert.equal(c.get('a'),undefined);assert.equal(c.size,0);
},
() => {
const c=new TTLCache<number>(2,()=>0);c.set('a',1,10);c.set('b',2,10);c.get('a');c.set('c',3,10);assert.equal(c.has('b'),false);assert.equal(c.has('a'),true);
},
() => {
let now=0;const c=new TTLCache<number>(2,()=>now);c.set('a',1,2);now=1;c.set('a',2,10);now=3;assert.equal(c.get('a'),2);c.set('a',3,0);assert.equal(c.has('a'),false);
},
() => {
assert.throws(()=>new TTLCache(-1,()=>0),RangeError);assert.throws(()=>new TTLCache(1.1,()=>0),RangeError);const c=new TTLCache<number>(1,()=>0);c.set('x',2,2);for(const n of [-1,NaN,Infinity]) assert.throws(()=>c.set('x',3,n),RangeError);assert.equal(c.get('x'),2);const z=new TTLCache(0,()=>0);z.set('x',1,10);assert.equal(z.size,0);
},
() => {
let now=0;const c=new TTLCache<undefined>(2,()=>now);c.set('u',undefined,3);assert.equal(c.has('u'),true);assert.equal(c.delete('u'),true);assert.equal(c.delete('u'),false);c.set('a',undefined,1);c.set('b',undefined,10);c.has('a');now=2;c.set('c',undefined,10);assert.equal(c.size,2);assert.equal(c.has('b'),true);
}];
let failed = 0;
groups.forEach((run, index) => {
  try { run(); console.log('OMNI_GROUP ' + JSON.stringify({group: index + 1, status: 'PASS'})); }
  catch (error) { failed++; console.log('OMNI_GROUP ' + JSON.stringify({group: index + 1, status: 'FAIL', error: error instanceof Error ? error.name : 'Error'})); }
});
console.log('OMNI_ACCEPTANCE_FINISHED');
process.exitCode = failed ? 1 : 0;
