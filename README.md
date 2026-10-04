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
- **New space, new spoke.** A space you open on the hub (prefix+c, the
  sidebar) becomes a new spoke: the plugin's `workspace.created` hook names one
  (`brisk-otter`), renames the space, and runs `spoke new <name> --in-pane` in
  it. That shows a 5-second grace period (any key keeps a plain hub shell),
  creates the VM from the golden image, and turns the pane into the spoke's main
  slot. Spaces herdr-machine0 opens itself, worktree spaces and restored
  sessions are left alone. Off with `"auto_spoke_on_new_space": false`.
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

## Requirements

- Hub: Linux or macOS, herdr 0.9.2+, python3 3.9+, node 20+, pi, the `machine0`
  CLI (`MACHINE0_API_TOKEN`), and an ssh key registered with machine0.
- Spoke: OpenSSH with streamlocal forwarding (`forward: tcp` covers sshds without
  it), `dtach`, python3, herdr (CLI only), and the harnesses.

The dotfiles' `m0/bin/bootstrap-m0 --role hub|spoke` sets all of this up.

## Commands

Hub:

```
spoke new <name> [--size large] [--repo owner/repo]... [--harness pi]
spoke ls | wake <name> | suspend <name> | ssh <name> [cmd] | keep-awake <name> [on|off]
spoke rm <name> [--force]            # refuses with unpushed work; archives sessions
spoke sync <name> | --running        # pull dotfiles and restow on spokes
spoke image build [--fresh]          # golden image m0-spoke (promote, keep 2 versions)
spoke attach <spoke> [slot] [--harness H] [--cwd DIR] [--takeover]
spoke popup                          # new agent / new spoke (herdr keybinding)
spoke secrets show | set KEY | login # hub secrets; `login` opens pi on the broker store
spoke token <provider>               # fresh access token (for hub-side tools)
spoke usage claude|codex
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
`herdr_machine0/config.py`: region, size, image, profile, the ssh key name,
idle minutes, load threshold, brokered providers, paste directories,
`forward` (`unix` or `tcp`), and `static_spokes` (non-machine0 hosts treated as
always running: `{name: {host, user, home}}`).

## Development

```
python3 -B -m unittest discover -s tests -t .
```

`docs/spike.md` records how the herdr and pi behaviour this depends on was
verified.
