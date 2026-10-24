// Runtime config written by CDK next to index.html (/config.json), so one build
// works for any stack. For local dev, copy the deployed config.json into public/.
export interface AppConfig { region: string; apiUrl: string; userPoolId: string; clientId: string; cognitoDomain: string }

let cfg: AppConfig | null = null;

export async function loadConfig(): Promise<void> {
  const r = await fetch('/config.json', { cache: 'no-store' });
  if (!r.ok) throw new Error('Missing /config.json');
  cfg = await r.json();
}

export function config(): AppConfig {
  if (!cfg) throw new Error('config not loaded');
  return cfg;
}
