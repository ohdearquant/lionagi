/** Simple S-curve bezier path for workflow edges (right-to-left port connection). */
export function bezierPath(x1: number, y1: number, x2: number, y2: number): string {
  const dx = Math.min(80, Math.max(32, Math.abs(x2 - x1) / 2));
  return `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2 - 5} ${y2}`;
}
