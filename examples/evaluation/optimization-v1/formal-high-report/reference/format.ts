import type {Summary} from './types.ts';
export function toCSV(rows:readonly Summary[]):string {
 const escape=(s:string)=>/[",\r\n]/.test(s)?'"'+s.replaceAll('"','""')+'"':s;
 return ['category,count,totalCents',...rows.map(r=>[escape(r.category),r.count,r.totalCents].join(','))].join('\n')+'\n';
}
