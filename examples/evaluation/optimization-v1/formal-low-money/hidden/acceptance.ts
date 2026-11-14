import assert from 'node:assert/strict';
import {sumCents} from './candidate.ts';
const groups: Array<() => void> = [
() => {
assert.equal(sumCents([]),0); assert.equal(sumCents(['1','2.50','0.1']),360);
},
() => {
assert.equal(sumCents(Array(100).fill('0.29')),2900); assert.equal(sumCents(['90071992547409.91']),Number.MAX_SAFE_INTEGER);
},
() => {
for(const v of ['1junk',' 1','1 ','01','1.001','+1','-1','NaN','1e2','']) assert.throws(()=>sumCents([v]),TypeError);
},
() => {
assert.throws(()=>sumCents(['90071992547409.92']),RangeError); assert.throws(()=>sumCents(['90071992547409.91','0.01']),RangeError);
},
() => {
const input=Object.freeze(['1.20','3.40']); assert.equal(sumCents(input),460); assert.deepEqual(input,['1.20','3.40']); assert.throws(()=>sumCents([1 as unknown as string]),TypeError);
}];
let failed = 0;
groups.forEach((run, index) => {
  try { run(); console.log('OMNI_GROUP ' + JSON.stringify({group: index + 1, status: 'PASS'})); }
  catch (error) { failed++; console.log('OMNI_GROUP ' + JSON.stringify({group: index + 1, status: 'FAIL', error: error instanceof Error ? error.name : 'Error'})); }
});
console.log('OMNI_ACCEPTANCE_FINISHED');
process.exitCode = failed ? 1 : 0;
