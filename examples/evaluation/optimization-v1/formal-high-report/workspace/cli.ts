import {readFileSync,writeFileSync,renameSync,existsSync,unlinkSync} from 'node:fs';
import {report} from './pipeline.ts';
const [input,format,output,minimum='0']=process.argv.slice(2);
let temp:string|undefined;
try {
 if(!input||!output||!['csv','json'].includes(format)||! /^(?:0|[1-9][0-9]*)$/.test(minimum))throw Error('args');
 const result=report(readFileSync(input,'utf8'),format as 'csv'|'json',Number(minimum));
 temp=output+'.temporary-'+process.pid;writeFileSync(temp,result,'utf8');renameSync(temp,output);
}catch{console.error('INVALID_REPORT');process.exitCode=2;}
finally{if(temp&&existsSync(temp))unlinkSync(temp);}
