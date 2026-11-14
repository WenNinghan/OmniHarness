import assert from 'node:assert/strict';
import {parseCSV,parseJSON} from './parse.ts';
import {summarize} from './stats.ts';
import {report} from './pipeline.ts';
import {toCSV} from './format.ts';
import {mkdtempSync,readFileSync,writeFileSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {spawnSync} from 'node:child_process';
const groups: Array<() => void> = [
() => {
const rows=[{category:'A',cents:2},{category:'A',cents:3}];assert.deepEqual(parseJSON(JSON.stringify(rows)),rows);assert.deepEqual(parseCSV('category,cents\nA,2\nA,3\n'),rows);assert.deepEqual(parseCSV('category,cents\r\n"A,B",2\r\n"C""D",3\r\n'),[{category:'A,B',cents:2},{category:'C"D',cents:3}]);
},
() => {
for(const text of ['null','[{"category":"A","cents":-1}]','[{"category":"","cents":1}]'])assert.throws(()=>parseJSON(text),TypeError);for(const text of ['category,cents\nA,1x','category,cents\nA,01','category,cents\nA,1,2','category,cents\n"A,2'])assert.throws(()=>parseCSV(text),TypeError);
},
() => {
const rows=Object.freeze([{category:'Z',cents:1},{category:'A',cents:2},{category:'A',cents:3},{category:'__proto__',cents:4}]);assert.deepEqual(summarize(rows,2),[{category:'A',count:2,totalCents:5},{category:'__proto__',count:1,totalCents:4}]);assert.equal(rows.length,4);assert.throws(()=>summarize(rows,-1),RangeError);assert.throws(()=>summarize([{category:'A',cents:Number.MAX_SAFE_INTEGER},{category:'A',cents:1}]),RangeError);
},
() => {
assert.equal(toCSV([]),'category,count,totalCents\n');assert.equal(toCSV([{category:'A,"B',count:2,totalCents:5}]),'category,count,totalCents\n"A,""B",2,5\n');assert.equal(report('category,cents\nA,2\nA,3\n','csv'),report('[{"category":"A","cents":2},{"category":"A","cents":3}]','json'));
},
() => {
const dir=mkdtempSync(join(tmpdir(),'report-'));try{const input=join(dir,'input.json'),output=join(dir,'out.csv');writeFileSync(input,'[{"category":"A","cents":3}]');let r=spawnSync(process.execPath,['--experimental-strip-types','cli.ts',input,'json',output],{encoding:'utf8'});assert.equal(r.status,0);assert.equal(readFileSync(output,'utf8'),'category,count,totalCents\nA,1,3\n');writeFileSync(output,'KEEP');writeFileSync(input,'null');r=spawnSync(process.execPath,['--experimental-strip-types','cli.ts',input,'json',output],{encoding:'utf8'});assert.equal(r.status,2);assert.equal(readFileSync(output,'utf8'),'KEEP');assert.match(r.stderr,/INVALID_REPORT/);}finally{rmSync(dir,{recursive:true,force:true});}
}];
let failed = 0;
groups.forEach((run, index) => {
  try { run(); console.log('OMNI_GROUP ' + JSON.stringify({group: index + 1, status: 'PASS'})); }
  catch (error) { failed++; console.log('OMNI_GROUP ' + JSON.stringify({group: index + 1, status: 'FAIL', error: error instanceof Error ? error.name : 'Error'})); }
});
console.log('OMNI_ACCEPTANCE_FINISHED');
process.exitCode = failed ? 1 : 0;
