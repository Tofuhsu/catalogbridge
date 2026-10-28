import { config } from './config';
import { idToken } from './auth';

export type Mapping = Partial<Record<'sku' | 'title' | 'price' | 'stock', string>>;
export const TARGETS = ['sku', 'title', 'price', 'stock'] as const;
// stock is optional since catalogbridge.v2 (suppliers that do not track inventory leave it empty).
export const REQUIRED_TARGETS: readonly string[] = ['sku', 'title', 'price'];
export const CURRENCIES = ['USD', 'EUR', 'GBP', 'CAD', 'AUD', 'TWD', 'JPY'];

export interface Job {
  jobId: string; status: string; supplierId: string; currency: string; fileName: string;
  createdAt: number; updatedAt: number; parentJobId?: string; rowCount?: number; headers?: string[];
  rejectReasons?: string[]; mappingVersion: number; mappingSource?: string; mapping: Mapping; mappingFrozen: boolean;
  expectedCount?: number; processedCount?: number; validCount?: number; invalidCount?: number;
  dispatchComplete?: boolean; progress: number; stalled: boolean; allowedActions: string[];
  aiSuggestion?: { mapping: Mapping; dropped: string[]; notes: string; modelId: string; latencyMs: number };
  completedAt?: number; runRequestedAt?: number; schemaVersion: string;
}
export interface Row { rowNumber: number; values: string[] }
export interface ResultItem {
  rowNumber: number; resultStatus: 'VALID' | 'INVALID';
  normalizedData?: Record<string, string>; sourceData?: Record<string, string>;
  validationErrors?: { field: string; code: string; message: string }[];
}
export interface Template { templateId: string; name: string; supplierId: string; mapping: Mapping; sourceHeaders: string[]; version: number }

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string, public details?: any) { super(message); }
}

async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
  const r = await fetch(`${config().apiUrl}${path}`, {
    method,
    headers: { Authorization: `Bearer ${await idToken()}`, ...(body ? { 'Content-Type': 'application/json' } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new ApiError(r.status, j?.error?.code ?? 'HTTP', j?.error?.message ?? `Request failed (${r.status})`, j?.error?.details);
  return j as T;
}

export const api = {
  createJob: (b: { clientToken: string; currency: string; supplierId: string; fileName: string; parentJobId?: string }) =>
    call<{ job: Job; upload?: { url: string; fields: Record<string, string>; maxBytes: number } }>('POST', '/jobs', b),
  listJobs: (cursor?: string) => call<{ jobs: Job[]; nextCursor?: string }>('GET', `/jobs${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''}`),
  getJob: (id: string) => call<{ job: Job }>('GET', `/jobs/${id}`),
  preview: (id: string, all = false) => call<{ headers: string[]; rows: Row[]; rowCount: number; parentMapping?: Mapping }>('GET', `/jobs/${id}/preview${all ? '?all=1' : ''}`),
  suggest: (id: string) => call<{ suggestion: NonNullable<Job['aiSuggestion']>; reused: boolean }>('POST', `/jobs/${id}/mapping-suggestion`),
  putMapping: (id: string, mapping: Mapping, mappingVersion: number, source: string, templateId?: string) =>
    call<{ job: Job }>('PUT', `/jobs/${id}/mapping`, { mapping, mappingVersion, source, templateId }),
  run: (id: string, runRequestId: string, mappingVersion: number) => call<{ job: Job }>('POST', `/jobs/${id}/run`, { runRequestId, mappingVersion }),
  results: (id: string, status: string, cursor?: string) =>
    call<{ results: ResultItem[]; nextCursor?: string }>('GET', `/jobs/${id}/results?status=${status}${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`),
  downloads: (id: string) => call<{ output?: string; errors?: string; original?: string }>('GET', `/jobs/${id}/downloads`),
  templates: () => call<{ templates: Template[] }>('GET', '/mapping-templates'),
  putTemplate: (id: string, b: { name: string; supplierId: string; mapping: Mapping; sourceHeaders: string[] }) =>
    call<{ template: Template }>('PUT', `/mapping-templates/${id}`, b),
};

export async function uploadFile(upload: { url: string; fields: Record<string, string> }, file: Blob): Promise<void> {
  const form = new FormData();
  Object.entries(upload.fields).forEach(([k, v]) => form.append(k, v));
  form.append('file', file); // must be the last field for S3 POST
  const r = await fetch(upload.url, { method: 'POST', body: form });
  if (!r.ok) throw new Error(r.status === 400 ? 'Upload rejected by storage (file too large or expired link).' : `Upload failed (${r.status})`);
}

export async function fetchAllInvalid(id: string): Promise<ResultItem[]> {
  const out: ResultItem[] = [];
  let cursor: string | undefined;
  do {
    const r = await api.results(id, 'INVALID', cursor);
    out.push(...r.results);
    cursor = r.nextCursor;
  } while (cursor);
  return out;
}

export function newToken(): string {
  return crypto.randomUUID();
}

export const TERMINAL = ['COMPLETED', 'COMPLETED_WITH_ERRORS', 'REJECTED'];
