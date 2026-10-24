// Cognito Hosted UI login with Authorization Code + PKCE (public SPA client, no secret).
// Tokens live in sessionStorage (cleared when the tab closes). They are never logged.
import { config } from './config';

const KEY = 'cb.tokens';
const VERIFIER = 'cb.pkce';
const STATE = 'cb.state';

interface Tokens { idToken: string; accessToken: string; refreshToken?: string; expiresAt: number }

function b64url(bytes: ArrayBuffer | Uint8Array): string {
  const arr = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  let s = '';
  arr.forEach((b) => (s += String.fromCharCode(b)));
  return btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function randomString(n = 48): string {
  return b64url(crypto.getRandomValues(new Uint8Array(n)));
}

function redirectUri(): string {
  return `${window.location.origin}/callback`;
}

export async function login(): Promise<void> {
  const verifier = randomString(64);
  const challenge = b64url(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier)));
  const state = randomString(16);
  sessionStorage.setItem(VERIFIER, verifier);
  sessionStorage.setItem(STATE, state);
  const p = new URLSearchParams({
    response_type: 'code', client_id: config().clientId, redirect_uri: redirectUri(),
    scope: 'openid email profile', code_challenge_method: 'S256', code_challenge: challenge, state,
  });
  window.location.assign(`https://${config().cognitoDomain}/oauth2/authorize?${p}`);
}

async function tokenRequest(body: Record<string, string>): Promise<Tokens> {
  const r = await fetch(`https://${config().cognitoDomain}/oauth2/token`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ client_id: config().clientId, ...body }),
  });
  if (!r.ok) throw new Error(`Sign-in failed (${r.status})`);
  const j = await r.json();
  return { idToken: j.id_token, accessToken: j.access_token, refreshToken: j.refresh_token ?? body.refresh_token,
    expiresAt: Date.now() + (j.expires_in - 60) * 1000 };
}

export async function handleCallback(url: URL): Promise<void> {
  const code = url.searchParams.get('code');
  const state = url.searchParams.get('state');
  const verifier = sessionStorage.getItem(VERIFIER);
  if (!code || !verifier || state !== sessionStorage.getItem(STATE)) throw new Error('Invalid sign-in response.');
  const t = await tokenRequest({ grant_type: 'authorization_code', code, redirect_uri: redirectUri(), code_verifier: verifier });
  sessionStorage.removeItem(VERIFIER);
  sessionStorage.removeItem(STATE);
  sessionStorage.setItem(KEY, JSON.stringify(t));
}

function stored(): Tokens | null {
  try { return JSON.parse(sessionStorage.getItem(KEY) || 'null'); } catch { return null; }
}

export function isLoggedIn(): boolean {
  return stored() !== null;
}

export function userEmail(): string {
  const t = stored();
  if (!t) return '';
  try { return JSON.parse(atob(t.idToken.split('.')[1].replace(/-/g, '+').replace(/_/g, '/'))).email ?? ''; } catch { return ''; }
}

// The API authorizer validates the ID token's audience (the app client id).
export async function idToken(): Promise<string> {
  const t = stored();
  if (!t) throw new Error('Not signed in');
  if (Date.now() < t.expiresAt) return t.idToken;
  if (!t.refreshToken) { logout(); throw new Error('Session expired'); }
  const fresh = await tokenRequest({ grant_type: 'refresh_token', refresh_token: t.refreshToken });
  sessionStorage.setItem(KEY, JSON.stringify(fresh));
  return fresh.idToken;
}

export function logout(): void {
  sessionStorage.removeItem(KEY);
  const p = new URLSearchParams({ client_id: config().clientId, logout_uri: `${window.location.origin}/` });
  window.location.assign(`https://${config().cognitoDomain}/logout?${p}`);
}
