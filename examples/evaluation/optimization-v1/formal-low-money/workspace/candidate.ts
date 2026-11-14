export function sumCents(values: readonly string[]): number {
  return Math.round(values.reduce((total, value) => total + parseFloat(value), 0) * 100);
}
