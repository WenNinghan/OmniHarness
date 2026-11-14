import type {Row} from './types.ts';
function amount(v:unknown):number {
 if(typeof v!=='number'||!Number.isSafeInteger(v)||v<0)throw new TypeError('cents');return v;
}
export function parseJSON(text:string):Row[] {
 const rows:unknown=JSON.parse(text);if(!Array.isArray(rows))throw new TypeError('rows');
 return rows.map((r:unknown)=>{if(typeof r!=='object'||r===null)throw new TypeError('row');const x=r as Record<string,unknown>;
 if(typeof x.category!=='string'||!x.category)throw new TypeError('category');return {category:x.category,cents:amount(x.cents)};});
}
export function parseCSV(text:string):Row[] {
 const records:string[][]=[];let record:string[]=[],field='',quoted=false,closed=false;
 for(let i=0;i<text.length;i++) {const c=text[i];
  if(quoted){if(c==='"'){if(text[i+1]==='"'){field+='"';i++;}else{quoted=false;closed=true;}}else field+=c;continue;}
  if(c==='"'){if(field||closed)throw new TypeError('csv');quoted=true;continue;}
  if(c===','||c==='\n'||c==='\r'){record.push(field);field='';closed=false;if(c!==','){records.push(record);record=[];if(c==='\r'&&text[i+1]==='\n')i++;}continue;}
  if(closed)throw new TypeError('csv');field+=c;
 }
 if(quoted)throw new TypeError('csv');if(field||record.length||closed){record.push(field);records.push(record);}
 if(JSON.stringify(records.shift())!==JSON.stringify(['category','cents']))throw new TypeError('header');
 return records.map(r=>{if(r.length!==2||!r[0]||!/^(?:0|[1-9][0-9]*)$/.test(r[1]))throw new TypeError('row');return {category:r[0],cents:amount(Number(r[1]))};});
}
