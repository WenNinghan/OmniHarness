import type {Row,Summary} from './types.ts';
export function summarize(rows:readonly Row[],minimum=0):Summary[] {
 const result:Record<string,Summary>={};
 for(const r of rows){if(r.cents<=minimum)continue;result[r.category]={category:r.category,count:1,totalCents:r.cents};}
 return Object.values(result);
}
