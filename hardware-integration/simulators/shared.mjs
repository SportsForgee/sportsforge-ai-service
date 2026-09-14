// Shared helpers for both hardware simulators — plain Node (fetch is built-in on
// Node 18+), no npm dependencies so the demo works with a bare `node` install.

export function parseArgs(argv) {
  const args = { api: "http://localhost:5186", interval: 2000, mode: "live" };
  for (let i = 0; i < argv.length; i++) {
    const key = argv[i];
    if (!key.startsWith("--")) continue;
    const name = key.slice(2);
    const value = argv[i + 1];
    args[name] = value;
    i++;
  }
  return args;
}

export async function login(api, email, password) {
  const res = await fetch(`${api}/api/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  if (!res.ok) {
    throw new Error(`Login failed (${res.status}): ${await res.text()}`);
  }
  const data = await res.json();
  return { token: data.token, name: data.name };
}

export async function post(api, path, token, body) {
  const res = await fetch(`${api}${path}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    throw new Error(`POST ${path} failed (${res.status}): ${await res.text()}`);
  }
  return res.json();
}

// Small random walk so values feel like a real signal instead of pure noise.
export function randomWalk(current, min, max, step) {
  const next = current + (Math.random() - 0.5) * step;
  return Math.min(max, Math.max(min, next));
}

export function jitter(base, spread) {
  return base + (Math.random() - 0.5) * spread;
}
