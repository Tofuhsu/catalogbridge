<script setup lang="ts">
import { ref } from 'vue';
import { useRouter } from 'vue-router';
import { api, uploadFile, newToken, CURRENCIES } from '../api';

const router = useRouter();
const supplier = ref('');
const currency = ref('USD');
const file = ref<File | null>(null);
const busy = ref(false);
const error = ref('');
let clientToken = newToken(); // reused if the user clicks twice or retries

function pick(e: Event) {
  const f = (e.target as HTMLInputElement).files?.[0] ?? null;
  error.value = '';
  // Checked before creating the job so a refused upload never leaves a CREATED job behind.
  if (f && f.size === 0) { error.value = 'File is empty.'; file.value = null; return; }
  if (f && f.size > 1_048_576) { error.value = 'File is larger than 1 MB.'; file.value = null; return; }
  file.value = f;
  clientToken = newToken();
}

async function submit() {
  if (!file.value || busy.value) return;
  busy.value = true;
  error.value = '';
  try {
    const r = await api.createJob({ clientToken, currency: currency.value, supplierId: supplier.value.trim(), fileName: file.value.name });
    if (r.upload) await uploadFile(r.upload, file.value);
    router.push(`/jobs/${r.job.jobId}`);
  } catch (e: any) {
    error.value = e.message;
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <h1>New job</h1>
  <div class="card">
    <p class="muted small">CSV, UTF-8 (BOM ok), up to 1 MB and 500 data rows, first row = column names.
      Output fields: sku, title, price, stock; currency is chosen here (no exchange-rate conversion).</p>
    <div class="row">
      <div><label for="sup">Supplier (optional)</label><input id="sup" v-model="supplier" maxlength="60" placeholder="e.g. acme" /></div>
      <div><label for="cur">Currency</label>
        <select id="cur" v-model="currency"><option v-for="c in CURRENCIES" :key="c">{{ c }}</option></select></div>
      <div><label for="file">CSV file</label><input id="file" type="file" accept=".csv,text/csv" @change="pick" /></div>
    </div>
    <div v-if="error" class="error">{{ error }}</div>
    <p><button class="btn btn-primary" :disabled="!file || busy" @click="submit">{{ busy ? 'Uploading...' : 'Upload' }}</button></p>
  </div>
</template>
