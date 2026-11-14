import {expiration} from './clock.ts';
import type {Clock} from './clock.ts';
export class TTLCache<V> {
  private values = new Map<string,{value:V,until:number}>();
  private capacity: number;
  private clock: Clock;
  constructor(capacity: number, clock: Clock) {
    if (!Number.isInteger(capacity) || capacity < 0) throw new RangeError('capacity');
    this.capacity=capacity; this.clock=clock;
  }
  private purge(): void { const now=this.clock(); for(const [k,v] of this.values) if(v.until<=now) this.values.delete(k); }
  set(key:string,value:V,ttl:number):void {
    const until=expiration(this.clock(),ttl); this.purge(); this.values.delete(key);
    if(this.capacity===0 || ttl===0) return;
    this.values.set(key,{value,until});
    while(this.values.size>this.capacity) this.values.delete(this.values.keys().next().value!);
  }
  get(key:string):V|undefined { this.purge(); const item=this.values.get(key); if(!item) return undefined; this.values.delete(key); this.values.set(key,item); return item.value; }
  has(key:string):boolean { this.purge(); return this.values.has(key); }
  delete(key:string):boolean { this.purge(); return this.values.delete(key); }
  get size():number { this.purge(); return this.values.size; }
}
