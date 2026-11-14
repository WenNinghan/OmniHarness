import type {Row,Summary} from './types.ts';
export function summarize(rows:readonly Row[],minimum=0):Summary[] {
 if(!Number.isSafeInteger(minimum)||minimum<0)throw new RangeError('minimum');
 const groups=new Map<string,Summary>();
 for(const r of rows){if(r.cents<minimum)continue;const prev=groups.get(r.category)||{category:r.category,count:0,totalCents:0};
 const total=prev.totalCents+r.cents;if(!Number.isSafeInteger(total))throw new RangeError('overflow');
 groups.set(r.category,{category:r.category,count:prev.count+1,totalCents:total});}
 return [...groups.values()].sort((a,b)=>a.category<b.category?-1:a.category>b.category?1:0);
}
