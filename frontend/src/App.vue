<script setup lang="ts">
import { computed } from 'vue';
import { useRoute } from 'vue-router';
import { isLoggedIn, logout, userEmail } from './auth';

const route = useRoute();
const signedIn = computed(() => (route.fullPath, isLoggedIn()));
</script>

<template>
  <header class="topbar">
    <RouterLink to="/jobs" class="brand">CatalogBridge</RouterLink>
    <nav v-if="signedIn">
      <RouterLink to="/jobs">Jobs</RouterLink>
      <RouterLink to="/jobs/new" class="btn btn-primary btn-sm">New job</RouterLink>
      <span class="muted">{{ userEmail() }}</span>
      <button class="btn btn-sm" @click="logout">Sign out</button>
    </nav>
  </header>
  <main class="container">
    <RouterView />
  </main>
  <footer class="container muted small">
    Demo project. Invited test accounts only. Files are deleted after 90 days.
  </footer>
</template>
