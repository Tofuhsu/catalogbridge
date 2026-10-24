<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { api, newToken, TARGETS, ApiError, type Job, type Mapping, type Row, type Template } from '../api';

const props = defineProps<{ job: Job }>();
const emit = defineEmits<{ (e: 'changed', job: Job): void }>();

const headers = ref<string[]>([]);
const rows = ref<Row[]>([]);
const mapping = ref<Mapping>({ ...props.job.mapping });
const source = ref(props.job.mappingSource ?? 'manual');
const templateId = ref<string | undefined>();
const templates = ref<Template[]>([]);
const msg = ref('');
const error = ref('');
const problems = ref<string[]>([]);
const aiBusy = ref(false);
const busy = ref(false);
const aiInfo = ref<Job['aiSuggestion'] | null>(props.job.aiSuggestion ?? null);
const runToken = newToken();

onMounted(async () => {
  try {
    const p = await api.preview(props.job.jobId);
    headers.value = p.headers;
    rows.value = p.rows;
    if (!Object.keys(mapping.value).length && p.parentMapping) {
      const ok = Object.values(p.parentMapping).every((c) => !c || p.headers.includes(c as string));
      if (ok) { mapping.value = { ...p.parentMapping }; source.value = 'parent'; msg.value = 'Mapping copied from the original job. Please confirm.'; }
    }
    templates.value = (await api.templates()).templates;
  } catch (e: any) { error.value = e.message; }
});

const usedTwice = computed(() => {
  const seen: Record<string, number> = {};
  Object.values(mapping.value).forEach((c) => c && (seen[c] = (seen[c] ?? 0) + 1));
  return Object.keys(seen).filter((c) => seen[c] > 1);
});
const complete = computed(() => TARGETS.every((t) => mapping.value[t]) && usedTwice.value.length === 0);

function setField(t: (typeof TARGETS)[number], v: string) {
  mapping.value = { ...mapping.value, [t]: v || undefined };
  source.value = 'manual';
}

async function suggest() {
  aiBusy.value = true; error.value = ''; msg.value = '';
  try {
    const r = await api.suggest(props.job.jobId);
    aiInfo.value = r.suggestion;
    mapping.value = { ...r.suggestion.mapping };
    source.value = 'ai';
    const missing = TARGETS.filter((t) => !r.suggestion.mapping[t]);
    msg.value = `AI suggestion applied${r.reused ? ' (saved suggestion reused)' : ''}. ` +
      (missing.length ? `Not mapped, please choose: ${missing.join(', ')}. ` : '') + 'Review before running.';
  } catch (e: any) {
    error.value = e instanceof ApiError && (e.code === 'AI_UNAVAILABLE' || e.code === 'AI_LIMIT')
      ? `${e.message}` : e.message;
  } finally { aiBusy.value = false; }
}

function applyTemplate(id: string) {
  const t = templates.value.find((x) => x.templateId === id);
  if (!t) return;
  const missing = Object.values(t.mapping).filter((c) => c && !headers.value.includes(c));
  const next: Mapping = {};
  TARGETS.forEach((f) => { const c = t.mapping[f]; if (c && headers.value.includes(c)) next[f] = c; });
  mapping.value = next;
  source.value = 'template';
  templateId.value = id;
  msg.value = missing.length ? '' : `Template "${t.name}" applied.`;
  error.value = missing.length ? `Template "${t.name}" expects column(s) not in this file: ${missing.join(', ')}. Choose them manually.` : '';
}

async function save(): Promise<Job | null> {
  problems.value = []; error.value = '';
  try {
    const r = await api.putMapping(props.job.jobId, mapping.value, props.job.mappingVersion, source.value, templateId.value);
    emit('changed', r.job);
    return r.job;
  } catch (e: any) {
    error.value = e.message; problems.value = e.details?.problems ?? [];
    return null;
  }
}

async function saveTemplate() {
  const id = (props.job.supplierId || 'template').toLowerCase().replace(/[^a-z0-9-]+/g, '-').replace(/^-+/, '').slice(0, 40) || 'template';
  try {
    await api.putTemplate(id, { name: props.job.supplierId || id, supplierId: props.job.supplierId, mapping: mapping.value, sourceHeaders: headers.value });
    templates.value = (await api.templates()).templates;
    msg.value = `Saved as template "${id}".`;
  } catch (e: any) { error.value = e.message; problems.value = e.details?.problems ?? []; }
}

async function run() {
  if (busy.value) return;
  busy.value = true;
  try {
    const saved = await save();
    if (!saved) return;
    const r = await api.run(props.job.jobId, runToken, saved.mappingVersion);
    emit('changed', r.job);
  } catch (e: any) {
    error.value = e.message; problems.value = e.details?.problems ?? [];
  } finally { busy.value = false; }
}
</script>

<template>
  <div class="card">
    <h2>1. Preview ({{ job.rowCount }} rows)</h2>
    <div class="table-wrap" style="max-height: 320px; overflow-y: auto">
      <table>
        <thead><tr><th>#</th><th v-for="h in headers" :key="h">{{ h }}</th></tr></thead>
        <tbody><tr v-for="r in rows" :key="r.rowNumber"><td class="muted">{{ r.rowNumber }}</td><td v-for="(v, i) in r.values" :key="i">{{ v }}</td></tr></tbody>
      </table>
    </div>
    <p class="muted small">Showing the first {{ rows.length }} rows.</p>
  </div>

  <div class="card">
    <h2>2. Map columns</h2>
    <div class="row" style="margin-bottom: 8px">
      <button class="btn" :disabled="aiBusy" @click="suggest">{{ aiBusy ? 'Asking AI...' : 'Suggest with AI' }}</button>
      <select v-if="templates.length" :value="templateId ?? ''" @change="applyTemplate(($event.target as HTMLSelectElement).value)">
        <option value="" disabled>Apply a saved template...</option>
        <option v-for="t in templates" :key="t.templateId" :value="t.templateId">{{ t.name }} (v{{ t.version }})</option>
      </select>
    </div>
    <p class="muted small">AI only sees column names and 3 sample rows. It can be wrong: nothing runs until you confirm.
      Manual mapping always works.</p>
    <table style="max-width: 560px">
      <thead><tr><th>Output field</th><th>Source column</th></tr></thead>
      <tbody>
        <tr v-for="t in TARGETS" :key="t">
          <td><b>{{ t }}</b> <span class="muted small">required</span></td>
          <td>
            <select :value="mapping[t] ?? ''" @change="setField(t, ($event.target as HTMLSelectElement).value)">
              <option value="">(not mapped)</option>
              <option v-for="h in headers" :key="h" :value="h">{{ h }}</option>
            </select>
            <span v-if="mapping[t] && usedTwice.includes(mapping[t]!)" class="small" style="color: var(--red)"> used twice</span>
          </td>
        </tr>
        <tr><td><b>currency</b></td><td>{{ job.currency }} <span class="muted small">(job setting)</span></td></tr>
      </tbody>
    </table>
    <p v-if="aiInfo" class="muted small">AI: {{ aiInfo.modelId }}, {{ aiInfo.latencyMs }} ms. {{ aiInfo.notes }}
      <span v-if="aiInfo.dropped?.length"> Discarded: {{ aiInfo.dropped.join('; ') }}</span></p>
    <div v-if="msg" class="ok">{{ msg }}</div>
    <div v-if="error" class="error">{{ error }}<ul v-if="problems.length"><li v-for="p in problems" :key="p">{{ p }}</li></ul></div>
    <div class="row">
      <button class="btn" @click="saveTemplate">Save as template</button>
      <button class="btn btn-primary" :disabled="!complete || busy" @click="run">{{ busy ? 'Starting...' : 'Confirm mapping and run' }}</button>
    </div>
  </div>
</template>
