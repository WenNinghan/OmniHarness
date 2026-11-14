import type {Clock} from './clock.ts';
export class TTLCache<V> {
 private values=new Map<string,V>();
 constructor(capacity:number,clock:Clock) {}
 set(key:string,value:V,ttl:number):void {this.values.set(key,value);}
 get(key:string):V|undefined {return this.values.get(key);}
 has(key:string):boolean {return this.values.get(key)!==undefined;}
 delete(key:string):boolean {return this.values.delete(key);}
 get size():number {return this.values.size;}
}
