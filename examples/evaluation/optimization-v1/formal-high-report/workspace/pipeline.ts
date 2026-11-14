import {parseCSV,parseJSON} from './parse.ts';
import {summarize} from './stats.ts';
import {toCSV} from './format.ts';
export function report(text:string,format:'csv'|'json',minimum=0):string {
 return toCSV(summarize(format==='csv'?parseCSV(text):parseJSON(text),minimum));
}
