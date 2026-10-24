<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { api, type Job } from '../api';

const jobs = ref<Job[]>([]);
const cursor = ref<string | undefined>();
const loading = ref(false);
const error = ref('');

async function load(more = false) {
  loading.value = true;
  error.value = '';
  try {
    const r = await api.listJobs(more ? cursor.value : undefined);
    jobs.value = more ? [...jobs.value, ...r.jobs] : r.jobs;
    cursor.value = r.nextCursor;
  } catch (e: any) {
    error.value = e.message;
  } finally {
    loading.value = false;
  }
}
onMounted(() => load());
const fmt = (t?: number) => (t ? new Date(t).toLocaleString() : '');
</script>

<template>
  <div class="row" style="justify-content: space-between">
    <h1>Your jobs</h1>
    <div><button class="btn btn-sm" @click="load()">Refresh</button></div>
  </div>
  <div v-if="error" class="error">{{ error }}</div>
  <div class="card table-wrap">
    <table>
      <thead><tr><th>Created</th><th>File</th><th>Supplier</th><th>Status</th><th>Rows</th><th>Valid / invalid</th></tr></thead>
      <tbody>
        <tr v-for="j in jobs" :key="j.jobId">
          <td><RouterLink :to="`/jobs/${j.jobId}`">{{ fmt(j.createdAt) }}</RouterLink></td>
          <td>{{ j.fileName }} <span v-if="j.parentJobId" class="badge">corrected</span></td>
          <td>{{ j.supplierId || '-' }}</td>
          <td><span class="badge" :class="j.status">{{ j.status }}</span> <span v-if="j.stalled" class="badge stalled">stalled</span></td>
          <td>{{ j.rowCount ?? '-' }}</td>
          <td>{{ j.validCount ?? '-' }} / {{ j.invalidCount ?? '-' }}</td>
        </tr>
        <tr v-if="!jobs.length && !loading"><td colspan="6" class="muted">No jobs yet. <RouterLink to="/jobs/new">Create one</RouterLink>.</td></tr>
      </tbody>
    </table>
    <button v-if="cursor" class="btn btn-sm" style="margin-top: 8px" :disabled="loading" @click="load(true)">Load more</button>
  </div>
</template>
