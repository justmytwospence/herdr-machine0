# herdr-machine0

One [herdr](https://herdr.dev) server (the **hub**) showing agents that run on
many small, per-project [machine0](https://machine0.io) VMs (**spokes**). Spokes
are cloned from a golden image, suspend themselves when idle, and wake when you
press Enter in their pane. Every subscription login lives on the hub.

```
Mac / phone ──herdr / Heeler──▶ hub (one herdr server, broker, hubd)
                                  │ pane = `spoke attach demo main`
                                  │ ssh -tt -R <slot socket>:<relay>
                                  ▼
                           spoke "demo" (machine0 VM)
                           `spoke run` → dtach → pi / claude / codex / opencode
```

## How it works

- **One pane per slot.** A hub pane runs `spoke attach <spoke> <slot>`, a small
  pty wrapper around `ssh -tt`. The agent runs on the spoke inside `dtach`, so it
  outlives the ssh connection: a hub restart or a network blip only detaches it.
- **Agent state.** The wrapper carries `HERDR_AGENT=<harness>` in its
  environment, so herdr applies that agent's screen detection to the pane. herdr
  cannot see through ssh, and on macOS it cannot read an ssh process's
  environment at all. The spoke's official herdr integrations report through the
  slot's **relay**: a socket reverse-forwarded over the same ssh connection,
  which allows only the reporting methods and always rewrites the pane to the
  wrapper's own. The attention-queue bridge and hooks work through the relay too.
- **Resume.** For pi and opencode the relay sets `resume_argv` to `spoke attach …`,
  so after a hub herdr restart herdr reattaches the pane instead of running
  `pi --session` on the hub. herdr would resume claude and codex with their
  built-in commands, so their session ids never reach it; `hubd` reattaches
  those panes, which it finds by their cwd (each slot pane's cwd is that slot's
  own directory).
- **Credentials.** See below. Claude and Codex never fall back to API billing.
- **One spoke per repo, one tab per worktree.** A space you open on the hub
  (prefix+c, the sidebar) asks which repo it is for: the repos `gh` lists on
  your spokes (cached, refreshed hourly), or any `owner/repo` you type. A repo
  with a spoke opens it here (or focuses its space when one is open); a new
  repo gets a spoke named after it, with the repo cloned to `~/Projects/<repo>`
  and its setup run. "scratch" makes a repo-less spoke; Esc keeps a plain hub
  shell. Worktrees live in `~/Projects/<repo>/.worktrees/<branch>`, one tab
  each: `spoke worktree <spoke> <branch>` on the hub, the popup's branch
  prompt, or `spoke open-slot` from a spoke. Spaces herdr-machine0 opens itself,
  worktree spaces and restored sessions are left alone; turn the whole
  behaviour off with `"auto_spoke_on_new_space": false`.
- **Repo setup.** After the clone and after each new worktree, the repo's own
  setup runs, if it has one: `.herdr-machine0/setup.sh` (executable), else
  `conductor.json`'s `scripts.setup` (with Conductor's
  `CONDUCTOR_ROOT_PATH`, `CONDUCTOR_WORKSPACE_PATH` and
  `CONDUCTOR_WORKSPACE_NAME` set). `repo_sizes` in the config picks a bigger VM
  for a heavy repo.
- **Auto-suspend.** `spoke hubd` suspends a spoke once every slot has been
  idle or done for 2 hours, with no attention-queue `bg` or `activity` token,
  a 15-minute load under 0.3, and keep-awake off. A suspended spoke's pane shows
  a wake screen: Enter runs `machine0 start` and reconnects, `q` closes the pane.
  Nothing wakes a spoke on its own, including a hub restart.
- **Pastes.** Inside a bracketed paste, absolute paths to hub files under
  `/tmp`, `/var/tmp` or `~/.local/state/herdr-machine0/inbox` are copied to the
  spoke and rewritten. That is where herdr's clipboard images and Heeler
  attachments land.

## Credentials

| What | How a spoke gets it |
|---|---|
| Claude subscription (pi, Claude Code) | `ANTHROPIC_OAUTH_TOKEN` / `CLAUDE_CODE_OAUTH_TOKEN`: one `claude setup-token` token (valid for a year, never rotated), pushed in `secrets.env`. pi-anthropic-auth shapes the requests as usual. |
| ChatGPT subscription (pi `openai-codex`), Radius | **Brokered.** The hub's broker store (`~/.config/herdr-machine0/broker/auth.json`) holds the only refresh token, and `broker/broker.mjs` (pi SDK) is the only thing that refreshes it. On a spoke, the managed pi extension re-registers these providers so that login and refresh ask the hub, through the relay, for an access token. Spokes store a sentinel refresh token. |
| Codex CLI | Best effort: `~/.codex/auth.json` is rewritten with a fresh access token on every attach. |
| Meta, TypeSafe/Jev | `MODEL_API_KEY`, `TYPESAFE_API_KEY` in `secrets.env` |
| Plan usage | `spoke usage claude|codex`: the hub fetches it with the broker's logins and caches it for 60 s. Spokes ask through the relay. |

Never use the broker's logins anywhere else. Refresh tokens rotate on use, so a
second refresher would log the broker out.

## Setup

On the hub (Linux or macOS with herdr 0.9.2+, python3 3.9+, node 20+, git):

```sh
git clone https://github.com/justmytwospence/herdr-machine0 ~/herdr-machine0
python3 ~/herdr-machine0/spoke.py setup hub   # role, `spoke` CLI, machine0 CLI,
                                              # broker SDK, ssh key, herdr plugin
spoke doctor                                  # what is left, and how to fix it
```

`spoke doctor` then walks you through the rest:

1. A machine0 account and API token (`MACHINE0_API_TOKEN` in the hub's environment).
2. `machine0 keys new m0-hub --type PUBLIC --publicKeyPath ~/.ssh/id_ed25519.pub --default`.
3. `machine0 profiles new m0`, and `machine0 integrations connect github -p m0` for
   private repos on spokes.
4. `claude setup-token`, then `spoke secrets set CLAUDE_CODE_OAUTH_TOKEN`.
5. `spoke secrets login` for the brokered logins (OpenAI Codex, Radius, plus
   Anthropic for the plan-usage gauges).
6. `spoke image build --fresh` for the golden image.

Spokes need nothing by hand. The image build pushes the plugin to the builder
VM and runs `setup/spoke.sh`, which installs dtach, the herdr CLI, pi, Claude
Code, Codex and opencode, the herdr integrations, the spoke pi extension and
an sshd setting. Spokes always run the hub's copy of the plugin: every spoke
creation and `spoke sync` pushes it again.

### Personal setup

Spokes built that way are plain. Two settings in
`~/.config/herdr-machine0/config.json` add your own setup, as shell commands
run on the spoke:

- `provision_command`: after `setup/spoke.sh` when the golden image (or a GPU
  spoke) is provisioned. For example, clone your dotfiles and run their
  bootstrap.
- `sync_command`: on every spoke creation and `spoke sync`. For example, pull
  your dotfiles. If it prints "already current", the sync phase says so.

Optional extras: a checkout of
[herdr-attention-queue](https://github.com/justmytwospence/herdr-attention-queue)
gets its pi bridge installed on spokes when `$HERDR_MACHINE0_ATTENTION_QUEUE`
points at it during `spoke install-pi-extension`. herdr plugins cannot bind
keys, so add any shortcuts to your hub's herdr config yourself, for example:

```toml
[[keys.command]]
key = "prefix+shift+n"
type = "popup"
command = '"$HOME/.local/bin/spoke" popup'
description = "machine0: new agent / new spoke"
```

## Commands

Hub:

```
spoke new [<name>] --repo owner/repo [--size large] [--harness pi]   # a repo's spoke
spoke new <name>                     # a scratch spoke
spoke worktree <spoke> <branch>      # a worktree of a repo spoke, as a new tab
spoke repos [list|refresh|import]    # the repos new spaces offer
spoke ls | wake <name> | suspend <name> | ssh <name> [cmd] | keep-awake <name> [on|off]
spoke rm <name> [--force]            # refuses with unpushed work; archives sessions
spoke sync <name> | --running        # pull dotfiles and restow on spokes
spoke image build [--fresh]          # golden image m0-spoke (built, then promoted)
spoke attach <spoke> [slot] [--harness H] [--cwd DIR] [--takeover]
spoke popup                          # new agent / new spoke (herdr keybinding)
spoke secrets show | set KEY | login # hub secrets; `login` opens pi on the broker store
spoke token <provider>               # fresh access token (for hub-side tools)
spoke usage claude|codex
spoke setup hub | doctor              # install, then check everything
spoke hub-status | reconcile [--all] | hubd [--ensure] | role [hub|spoke]
```

Spoke:

```
spoke run <slot> --harness H --cwd DIR --pane ID   # what the wrapper runs over ssh
spoke open-slot --cwd DIR [--label L]              # open another hub pane on this spoke
spoke usage claude|codex
spoke install-pi-extension
```

herdr actions (plugin id `machine0`): `suspend-focused`, `wake-focused`,
`keep-awake`, `status`, `reconcile`, plus a `board` pane.

## Configuration

`~/.config/herdr-machine0/config.json` overrides the defaults in
`herdr_machine0/config.py`: `provision_command` and `sync_command` (above),
region, size, image, profile, the ssh key name,
idle minutes, load threshold, brokered providers, paste directories,
`forward` (`unix` or `tcp`), and `static_spokes` (non-machine0 hosts treated as
always running: `{name: {host, user, home}}`).

## Development

```
python3 -B -m unittest discover -s tests -t .
```

`docs/spike.md` records how the herdr and pi behaviour this depends on was
verified.
