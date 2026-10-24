<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import { handleCallback } from '../auth';

const router = useRouter();
const error = ref('');
onMounted(async () => {
  try {
    await handleCallback(new URL(window.location.href));
    router.replace('/jobs');
  } catch (e: any) {
    error.value = e.message;
  }
});
</script>

<template>
  <div class="card">
    <p v-if="!error">Signing in...</p>
    <div v-else class="error">{{ error }} <RouterLink to="/login">Try again</RouterLink></div>
  </div>
</template>
