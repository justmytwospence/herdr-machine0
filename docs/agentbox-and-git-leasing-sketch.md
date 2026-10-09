# Sketch: a machine0 provider for agentbox, and leased git tokens for spokes

Status: design sketch, not implemented. Based on agentbox at 605ee75
(github.com/madarco/agentbox, MIT): `docs/provider-plugins.md`,
`docs/cloud-providers.md`, `packages/core/src/cloud-backend.ts`,
`packages/relay/src/cloud-keepalive.ts`, `packages/sandbox-digitalocean`.

## 1. `agentbox-provider-machine0`

A provider plugin is an npm package built only on `@madarco/agentbox-provider-sdk`,
added with `agentbox plugin add agentbox-provider-machine0`. It exports a
`providerModule` whose core is a `CloudBackend` (about 13 methods);
`createCloudProvider(backend)` supplies everything else (workspace seeding, the
in-box supervisor, relay wiring, preview URLs, checkpoints, cp).

Start from the DigitalOcean backend: machine0 VMs are DigitalOcean droplets
underneath, run OpenSSH and cloud-init, and agentbox's DO backend already does
all I/O over one ssh ControlMaster per box. Replace its DO API calls with the
machine0 CLI (or API) and keep the ssh layer.

### Method mapping

| CloudBackend | machine0 | Notes |
|---|---|---|
| `provision(req)` | `machine0 new <name> --size --region --image --key --profile` | Return `{sandboxId, publicHost, resources}`. agentbox mints a per-box ed25519 key; machine0 takes a registered key name, so either register one per box (`machine0 keys new`, removed on destroy) or use one plugin-wide key. |
| `get` / `state` | `machine0 ls --json` | RUNNING -> `running`; STARTING/SUSPENDING -> `running` (transitional, as Hetzner does); SUSPENDED/STOPPED -> `paused`; absent -> `missing`. |
| `pause` | `machine0 suspend` | The point of the plugin: memory-preserving and billing-stopping, unlike Hetzner/DO power-off. |
| `resume` / `start` | `machine0 start` | Then refresh `publicHost` and the host key: machine0 gives a new IP on every resume. This is the main thing the DO code does not expect (it caches the IP in the box record). Do it in `resume` and in `repairReachability`. |
| `stop` | `machine0 stop` | |
| `destroy` | `machine0 rm` | Also drop the per-box key and known_hosts entry. |
| `exec`, `uploadFile`, `downloadFile`, `listFiles` | ssh / scp through the ControlMaster | Copy from the DO backend. Set `stageFilesAsRoot` if exec runs as the unprivileged user. |
| `previewUrl(port)` | `ssh -L 127.0.0.1:<local>:127.0.0.1:<port>` | Copy from the DO backend. |
| `createSnapshot` / `deleteSnapshot` | `machine0 images save` (needs a stopped VM), `images versions` | Optional; ship v1 with `checkpoints: false`. |
| `renewTimeout` | none | machine0 has no session TTL. |
| `setInbound` | none | No per-box firewall; set `inbound: false`. |

### Descriptor

```ts
capabilities: {
  pauseSemantics: 'freeze', checkpoints: false, ssh: true, persistentSsh: true,
  directBoxSsh: true, inbound: false, directGit: true, resync: true, prune: true,
  vnc: true, dind: true, hubRoutable: true,
},
timeoutModel: 'inactivity',
```

`timeoutModel: 'inactivity'` is what makes the host relay's keepalive loop
(`cloud-keepalive.ts`) pause a box whose agent has been idle for the box's own
window. That window comes from `box.cloud.sessionTimeoutMs`, set at create.
**Verify** how a plugin provider gets a non-zero `timeoutMs` at create: for a
record without one the loop falls back to `defaultFallbackCreateTimeoutMs`,
which reads `box.vercelTimeoutMs`. The plugin may need its own config key
(e.g. `box.machine0IdleMs`, default 120 min) threaded into provision.

### Base image (`prepare`)

Bake once per agentbox release: boot `ubuntu-24-04-loaded`, run
`seedAgentStaticIntoCloudBox` (agentbox's provider-neutral upload of the box
runtime: agentbox-ctl, Claude Code, Codex, opencode, pi, tmux, VNC), stop,
`machine0 images save`, promote. Record the image in the plugin's prepared
state. The dotfiles' chezmoi step can run here too.

### What this would and would not give you

- Gets: agentbox's hook-driven idle detection, approvals, the hub dashboard and
  REST API, checkpoints later, its herdr overlay plugin, and machine0's real
  suspend under all of it.
- Loses: the brokered-credential invariant. agentbox copies each agent's login
  file into the box with its refresh token (its notes report that OpenAI does
  not invalidate the previous refresh token on rotation; unverified here), and
  pi cannot refresh a borrowed Codex token. Fixing that is core agentbox work,
  not provider work.
- Loses: the Paseo front-end, and herdr-machine0's Enter-to-wake pane, paste
  rewriting and relay-reported agent state (agentbox reports state to herdr
  itself, see its `docs/terminal-integration.md`).
- Effort: the Hetzner and DigitalOcean backends together are about 10k lines
  with tests; a machine0 backend copied from DO is likely 1-2k, validated with
  agentbox's mock backend and contract tests (`cloud-providers.md` section 7.1).

## 2. Leased git tokens for spokes (both plugins)

Today the machine0 profile's GitHub integration gives every spoke a `gh` login
at boot, reaching every repo the integration can see, for as long as the VM
lives. agentbox's hub instead holds a GitHub App and leases one-hour,
single-repo installation tokens. The same fits both hubs.

### Hub side

- One GitHub App on your account (contents: read/write, pull requests:
  read/write, metadata: read), installed on the repos spokes work on. Its
  private key lives in the hub's secrets, never on a spoke.
- `mint(repo)`: sign an RS256 JWT with the App key (`openssl dgst -sha256
  -sign` or node, which the broker already needs), then
  `POST /app/installations/{id}/access_tokens` with
  `{"repositories": ["<repo>"], "permissions": {...}}`. Returns a token valid
  for one hour. Cache per repo; re-mint under 10 minutes left.
- Authorization: a spoke may only get tokens for the repos the hub registered
  for it (herdr-machine0: `registry.spokes[name].repo`; paseo-machine0: the
  `--repo` list given to `new`).

### Spoke side

- A git credential helper, `spoke git-credential` / `paseo-machine0 spoke
  git-credential`, set for `https://github.com` in the machine0 `.gitconfig.local`
  in place of `gh auth git-credential`. It answers `get` with
  `username=x-access-token` and the leased token.
- `gh` reads `GH_TOKEN`; the helper also writes the current token to a 0600 file
  that a small `gh` wrapper exports, or the hub pushes it into `secrets.env`.

### Delivery, per plugin

- **herdr-machine0 (pull):** the helper asks the slot's relay, a new
  `herdr_machine0.git_token {repo}` handler next to `herdr_machine0.credential`.
  Same limitation as the broker: it only works while a hub pane is attached.
- **paseo-machine0 (push; spokes never call the hub):** hubd adds a
  `git_tokens` map to the credential bundle and pushes when a token is under 20
  minutes from expiry; `apply-creds` writes it; the helper reads it.

### Then

Drop the GitHub integration from the spoke profiles (keep it only if `gh repo
list` for the new-space picker needs it; that call can move to the hub with the
App token). A leaked spoke token then reaches one repo for at most an hour.
