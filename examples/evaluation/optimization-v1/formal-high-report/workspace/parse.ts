import type {Row} from './types.ts';
export function parseJSON(text:string):Row[]{return JSON.parse(text) as Row[];}
export function parseCSV(text:string):Row[]{return text.trim().split('\n').slice(1).map(x=>{const [category,cents]=x.split(',');return {category,cents:parseInt(cents)};});}
