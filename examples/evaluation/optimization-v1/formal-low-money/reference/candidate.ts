export function sumCents(values: readonly string[]): number {
  let total = 0n;
  for (const value of values) {
    if (typeof value !== 'string' || !/^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,2})?$/.test(value)) throw new TypeError('amount');
    const [whole, fraction = ''] = value.split('.');
    const cents = BigInt(whole) * 100n + BigInt(fraction.padEnd(2, '0'));
    total += cents;
    if (total > BigInt(Number.MAX_SAFE_INTEGER)) throw new RangeError('overflow');
  }
  return Number(total);
}
