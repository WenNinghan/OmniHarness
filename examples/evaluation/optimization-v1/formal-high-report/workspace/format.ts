import type {Summary} from './types.ts';
export function toCSV(rows:readonly Summary[]):string {
 return 'category,count,totalCents\n'+rows.map(r=>`${r.category},${r.count},${r.totalCents}`).join('\n')+'\n';
}
