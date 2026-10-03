// herdr-machine0's pi extension for spokes. `spoke install-pi-extension` copies
// it to ~/.pi/agent/extensions/herdr-machine0.ts (managed: edit the plugin's
// pi/herdr-machine0.ts instead).
//
// Subscription logins rotate their refresh tokens, so a refresh token can live
// in exactly one place: the hub's credential broker. On a spoke this extension
// re-registers each brokered provider's OAuth so that login and refresh fetch a
// fresh access token from the hub, through the slot's relay socket
// (HERDR_SOCKET_PATH), instead of using a refresh token. auth.json on a spoke
// only ever holds a sentinel refresh token.
//
// Inert outside a spoke slot (no HERDR_MACHINE0_ROLE=spoke or no socket).
import net from "node:net";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const NAMES: Record<string, string> = {
  "openai-codex": "OpenAI (ChatGPT Plus/Pro) via hub",
  radius: "Radius via hub",
  anthropic: "Anthropic via hub",
};
const SUBSCRIPTION = new Set(["openai-codex", "anthropic"]);
const TIMEOUT_MS = 90_000;

type Credentials = { access: string; refresh: string; expires: number; [key: string]: unknown };

export function brokered(env: NodeJS.ProcessEnv = process.env): string[] {
  if (env.HERDR_MACHINE0_ROLE !== "spoke" || !env.HERDR_SOCKET_PATH) return [];
  const list = env.HERDR_MACHINE0_BROKERED ?? "openai-codex,radius";
  return list.split(",").map((s) => s.trim()).filter(Boolean);
}

export function request(socketPath: string, method: string, params: Record<string, unknown>): Promise<any> {
  return new Promise((resolve, reject) => {
    let buf = "";
    let done = false;
    const sock = net.createConnection(socketPath);
    const finish = (fn: () => void) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      sock.destroy();
      fn();
    };
    const timer = setTimeout(() => finish(() => reject(new Error(`${method}: hub did not answer`))), TIMEOUT_MS);
    sock.on("error", (err) => finish(() => reject(new Error(`${method}: hub unreachable (${err.message})`))));
    sock.on("connect", () => sock.write(JSON.stringify({ id: `pi:${method}:${Date.now()}`, method, params }) + "\n"));
    sock.on("data", (chunk) => {
      buf += chunk.toString();
      const nl = buf.indexOf("\n");
      if (nl < 0) return;
      const line = buf.slice(0, nl);
      finish(() => {
        try {
          const reply = JSON.parse(line);
          if (reply.error) reject(new Error(`${method}: ${reply.error.message ?? reply.error.code}`));
          else resolve(reply.result);
        } catch (err) {
          reject(err as Error);
        }
      });
    });
  });
}

export function fromHub(provider: string, env: NodeJS.ProcessEnv = process.env): () => Promise<Credentials> {
  return async () => {
    const socketPath = env.HERDR_SOCKET_PATH;
    if (!socketPath) throw new Error("not in a spoke slot: no hub to ask for credentials");
    const cred = await request(socketPath, "herdr_machine0.credential", { provider });
    if (!cred || typeof cred.access !== "string") throw new Error(`hub returned no ${provider} credential`);
    return cred as Credentials;
  };
}

export default function herdrMachine0(pi: ExtensionAPI) {
  for (const provider of brokered()) {
    const fetchCredential = fromHub(provider);
    pi.registerProvider(provider, {
      oauth: {
        name: NAMES[provider] ?? `${provider} via hub`,
        isSubscription: SUBSCRIPTION.has(provider),
        login: async () => fetchCredential(),
        refreshToken: async () => fetchCredential(),
        getApiKey: (credentials) => (credentials as Credentials).access,
      },
    });
  }
}
