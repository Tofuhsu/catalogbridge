// RFC 4180 writer used to build a corrected file in the browser.
export function toCsv(headers: string[], rows: string[][]): string {
  const cell = (v: string) => (/[",\r\n]/.test(v) ? `"${v.replace(/"/g, '""')}"` : v);
  return [headers, ...rows].map((r) => r.map((v) => cell(v ?? '')).join(',')).join('\r\n') + '\r\n';
}

export function utf8Size(s: string): number {
  return new TextEncoder().encode(s).length;
}
