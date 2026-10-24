import { createRouter, createWebHistory } from 'vue-router';
import { isLoggedIn } from './auth';
import LoginView from './views/LoginView.vue';
import CallbackView from './views/CallbackView.vue';
import JobsView from './views/JobsView.vue';
import NewJobView from './views/NewJobView.vue';
import JobDetailView from './views/JobDetailView.vue';

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/jobs' },
    { path: '/login', component: LoginView, meta: { public: true } },
    { path: '/callback', component: CallbackView, meta: { public: true } },
    { path: '/jobs', component: JobsView },
    { path: '/jobs/new', component: NewJobView },
    { path: '/jobs/:id', component: JobDetailView, props: true },
  ],
});

router.beforeEach((to) => {
  if (!to.meta.public && !isLoggedIn()) return '/login';
});
