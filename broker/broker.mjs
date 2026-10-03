#!/usr/bin/env node
// herdr-machine0 credential broker (runs on the hub only).
//
//   node broker.mjs <pi-package-dir> <broker-dir> get <provider> <min-validity-ms>
//   node broker.mjs <pi-package-dir> <broker-dir> status
//
// The broker's credential store is <broker-dir>/auth.json, written by a normal
// `pi /login` run with PI_CODING_AGENT_DIR=<broker-dir> (`spoke secrets login`).
// It is the only place a subscription refresh token lives, and this script is
// the only thing that refreshes it, so rotating refresh tokens never race.
// Callers serialize invocations with a lock. Spokes receive the access token
// and expiry only; the refresh token is replaced by a sentinel.
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

const SENTINEL = "herdr-machine0-broker";
const [piDir, dir, command, provider, minMs] = process.argv.slice(2);

function fail(message) {
  process.stderr.write(`broker: ${message}\n`);
  process.exit(1);
}

if (!piDir || !dir || !command) fail("usage: broker.mjs <pi-dir> <broker-dir> get <provider> <min-ms> | status");

const authPath = join(dir, "auth.json");
const readStore = () => {
  try {
    return JSON.parse(readFileSync(authPath, "utf8"));
  } catch {
    return {};
  }
};

if (command === "status") {
  const store = readStore();
  const out = {};
  for (const [id, cred] of Object.entries(store)) {
    out[id] = { type: cred?.type, expires: cred?.expires ?? null };
  }
  process.stdout.write(JSON.stringify(out) + "\n");
  process.exit(0);
}

if (command !== "get" || !provider) fail("unknown command");

const sdk = await import(pathToFileURL(join(piDir, "dist", "index.js")).href);
const runtime = await sdk.ModelRuntime.create({ authPath, modelsPath: null, refreshOnCreate: false });
const min = Number(minMs) > 0 ? Number(minMs) : 24 * 3600 * 1000;
// getAuth refreshes (and persists) when less than `min` remains.
const auth = await runtime.getAuth(provider, { minOAuthValidityMs: min });
if (!auth) fail(`no credential for ${provider}; run: spoke secrets login`);
const cred = readStore()[provider];
if (!cred || cred.type !== "oauth") fail(`${provider} is not an OAuth login in the broker store`);
const { refresh: _refresh, ...rest } = cred;
process.stdout.write(JSON.stringify({ ...rest, refresh: SENTINEL }) + "\n");
