export type Clock = () => number;
export function expiration(now: number, ttl: number): number {
  if (!Number.isFinite(ttl) || ttl < 0) throw new RangeError('ttl');
  return now + ttl;
}
