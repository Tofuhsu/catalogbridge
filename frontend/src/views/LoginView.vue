<script setup lang="ts">
import { login } from '../auth';
</script>

<template>
  <div class="card" style="max-width: 520px; margin: 40px auto">
    <h1>CatalogBridge</h1>
    <p>Turn supplier product CSVs with different column names into one validated catalog file
      (<code>catalogbridge.v1</code>), with an error report you can fix and re-run.</p>
    <p class="muted small">Invited test accounts only. Do not upload private or personal data.</p>
    <button class="btn btn-primary" @click="login">Sign in</button>
  </div>
</template>
