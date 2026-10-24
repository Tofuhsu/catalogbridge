<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { useRouter } from 'vue-router';
import { api, fetchAllInvalid, uploadFile, newToken, TERMINAL, TARGETS, type Job, type ResultItem } from '../api';
import { toCsv, utf8Size } from '../csv';
import MappingStep from '../components/MappingStep.vue';

const props = defineProps<{ id: string }>();
const router = useRouter();
const job = ref<Job | null>(null);
const error = ref('');
const invalid = ref<ResultItem[]>([]);
const edits = ref<Record<number, Record<string, string>>>({});
const drop = ref<Record<number, boolean>>({});
const fixBusy = ref(false);
let timer: number | undefined;
let delay = 3000;
let stopped = false;

// Poll every 3 s while the job is moving; stop on terminal states, on leaving the
// page, or while waiting for the user (AWAITING_MAPPING). Back off on errors.
async function poll() {
  try {
    job.value = (await api.getJob(props.id)).job;
    error.value = '';
    delay = 3000;
  } catch (e: any) {
    error.value = e.message;
    delay = Math.min(delay * 2, 30000);
    if (e.status === 404) return;
  }
  const s = job.value?.status;
  if (!stopped && s && !TERMINAL.includes(s) && s !== 'AWAITING_MAPPING') timer = window.setTimeout(poll, delay);
}
function restart() { window.clearTimeout(timer); delay = 3000; poll(); }
watch(() => props.id, () => { job.value = null; invalid.value = []; restart(); }, { immediate: true });
onBeforeUnmount(() => { stopped = true; window.clearTimeout(timer); });

function onChanged(j: Job) { job.value = j; if (!['AWAITING_MAPPING', ...TERMINAL].includes(j.status)) restart(); }

watch(() => job.value?.status, async (s) => {
  if (s === 'COMPLETED_WITH_ERRORS') {
    try { invalid.value = await fetchAllInvalid(props.id); } catch (e: any) { error.value = e.message; }
  }
});

const pct = computed(() => Math.round((job.value?.progress ?? 0) * 100));
const fmt = (t?: number) => (t ? new Date(t).toLocaleString() : '-');
const seconds = computed(() => job.value?.completedAt && job.value.runRequestedAt
  ? ((job.value.completedAt - job.value.runRequestedAt) / 1000).toFixed(1) : null);

async function download(kind: 'output' | 'errors' | 'original') {
  try {
    const d = await api.downloads(props.id);
    const url = d[kind];
    if (url) window.location.assign(url);
  } catch (e: any) { error.value = e.message; }
}

function editValue(row: number, target: string, fallback: string) { return edits.value[row]?.[target] ?? fallback; }
function setEdit(row: number, target: string, v: string) { edits.value = { ...edits.value, [row]: { ...(edits.value[row] ?? {}), [target]: v } }; }

// Corrected job = a NEW job built from the original rows + the user's edits.
// The finished job is never modified, so its results and counts stay as they were.
async function createCorrected() {
  if (!job.value || fixBusy.value) return;
  fixBusy.value = true; error.value = '';
  try {
    const all = await api.preview(props.id, true);
    const m = job.value.mapping;
    const out: string[][] = [];
    for (const r of all.rows) {
      if (drop.value[r.rowNumber]) continue;
      const values = [...r.values];
      const e = edits.value[r.rowNumber] ?? {};
      TARGETS.forEach((t) => {
        const col = m[t];
        if (col && e[t] !== undefined) values[all.headers.indexOf(col)] = e[t];
      });
      out.push(values);
    }
    if (!out.length) throw new Error('All rows were removed.');
    const text = toCsv(all.headers, out);
    if (utf8Size(text) > 1_048_576) throw new Error('Corrected file would exceed 1 MB.');
    const name = job.value.fileName.replace(/(\.csv)?$/i, '-corrected.csv');
    const r = await api.createJob({ clientToken: newToken(), currency: job.value.currency, supplierId: job.value.supplierId,
      fileName: name, parentJobId: job.value.jobId });
    if (r.upload) await uploadFile(r.upload, new Blob([text], { type: 'text/csv' }));
    router.push(`/jobs/${r.job.jobId}`);
  } catch (e: any) { error.value = e.message; } finally { fixBusy.value = false; }
}
</script>

<template>
  <div v-if="error" class="error">{{ error }}</div>
  <template v-if="job">
    <div class="row" style="justify-content: space-between">
      <h1>{{ job.fileName }}</h1>
      <div>
        <span class="badge" :class="job.status">{{ job.status }}</span>
        <span v-if="job.stalled" class="badge stalled">stalled: no progress for 10+ min, operator alerted</span>
      </div>
    </div>
    <p class="muted small">Job {{ job.jobId }} · supplier {{ job.supplierId || '-' }} · {{ job.currency }} · created {{ fmt(job.createdAt) }}
      <span v-if="job.parentJobId"> · corrected from <RouterLink :to="`/jobs/${job.parentJobId}`">{{ job.parentJobId.slice(0, 8) }}</RouterLink></span></p>

    <div v-if="job.status === 'CREATED' || job.status === 'VALIDATING_UPLOAD'" class="card">
      <p>{{ job.status === 'CREATED' ? 'Waiting for the upload to arrive...' : 'Checking the file structure...' }}</p>
    </div>

    <div v-if="job.status === 'REJECTED'" class="card">
      <h2>File rejected before processing</h2>
      <div class="error"><ul><li v-for="r in job.rejectReasons" :key="r">{{ r }}</li></ul></div>
      <p class="muted small">Nothing was processed. Fix the file and create a new job.</p>
      <RouterLink to="/jobs/new" class="btn btn-primary">New job</RouterLink>
    </div>

    <MappingStep v-if="job.status === 'AWAITING_MAPPING'" :key="job.jobId" :job="job" @changed="onChanged" />

    <div v-if="['RUN_REQUESTED', 'PROCESSING', 'REPORTING', 'COMPLETED', 'COMPLETED_WITH_ERRORS'].includes(job.status)" class="card">
      <h2>Processing</h2>
      <div class="progress"><div :style="{ width: pct + '%' }"></div></div>
      <div class="grid4" style="margin-top: 12px">
        <div class="stat"><b>{{ job.processedCount ?? 0 }} / {{ job.expectedCount ?? '-' }}</b>rows processed</div>
        <div class="stat"><b>{{ job.validCount ?? 0 }}</b>valid</div>
        <div class="stat"><b>{{ job.invalidCount ?? 0 }}</b>invalid</div>
        <div class="stat"><b>{{ seconds ?? '-' }}</b>seconds end to end</div>
      </div>
      <p class="muted small">Mapping (frozen): {{ TARGETS.map((t) => `${t} ← ${job!.mapping[t]}`).join(' · ') }} · source: {{ job.mappingSource }}</p>
    </div>

    <div v-if="job.status === 'COMPLETED' || job.status === 'COMPLETED_WITH_ERRORS'" class="card">
      <h2>Downloads</h2>
      <div class="row">
        <button class="btn btn-primary" @click="download('output')">Download {{ job.schemaVersion }} CSV</button>
        <button class="btn" @click="download('errors')">Download error report</button>
        <button class="btn" @click="download('original')">Download original</button>
      </div>
      <p class="muted small">Output contains valid rows only. Text cells starting with = + - @ are prefixed with ' so spreadsheets do not run them as formulas.</p>
    </div>

    <div v-if="job.status === 'COMPLETED_WITH_ERRORS'" class="card">
      <h2>Fix invalid rows ({{ invalid.length }})</h2>
      <p class="muted small">Edit values or drop rows, then create a corrected job. This job stays unchanged.</p>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Row</th><th v-for="t in TARGETS" :key="t">{{ t }}</th><th>Problems</th><th>Drop</th></tr></thead>
          <tbody>
            <tr v-for="r in invalid" :key="r.rowNumber" :style="{ opacity: drop[r.rowNumber] ? 0.4 : 1 }">
              <td>{{ r.rowNumber }}</td>
              <td v-for="t in TARGETS" :key="t">
                <input :value="editValue(r.rowNumber, t, r.sourceData?.[t] ?? '')"
                  :style="{ borderColor: r.validationErrors?.some((e) => e.field === t) ? 'var(--red)' : undefined }"
                  @input="setEdit(r.rowNumber, t, ($event.target as HTMLInputElement).value)" />
              </td>
              <td class="small">{{ r.validationErrors?.map((e) => `${e.field}: ${e.message}`).join(' ') }}</td>
              <td><input v-model="drop[r.rowNumber]" type="checkbox" /></td>
            </tr>
          </tbody>
        </table>
      </div>
      <p><button class="btn btn-primary" :disabled="fixBusy" @click="createCorrected">{{ fixBusy ? 'Creating...' : 'Create corrected job' }}</button></p>
    </div>
  </template>
  <p v-else-if="!error">Loading...</p>
</template>
