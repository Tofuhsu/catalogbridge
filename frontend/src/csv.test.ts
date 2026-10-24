import { describe, expect, it } from 'vitest';
import { toCsv } from './csv';

describe('toCsv', () => {
  it('quotes commas, quotes and newlines (RFC 4180)', () => {
    expect(toCsv(['a', 'b'], [['x,y', 'say "hi"'], ['line\nbreak', '']]))
      .toBe('a,b\r\n"x,y","say ""hi"""\r\n"line\nbreak",\r\n');
  });
  it('keeps plain values unquoted', () => {
    expect(toCsv(['sku'], [['A1']])).toBe('sku\r\nA1\r\n');
  });
});
