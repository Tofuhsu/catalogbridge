import { createApp } from 'vue';
import App from './App.vue';
import { router } from './router';
import { loadConfig } from './config';
import './style.css';

loadConfig()
  .then(() => createApp(App).use(router).mount('#app'))
  .catch(() => { document.getElementById('app')!.textContent = 'Configuration could not be loaded.'; });
