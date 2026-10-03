# Spike results

Run 2026-10-03 with herdr 0.9.3 and pi 1.0.0. The hub stand-in was a named herdr
session on the Mac. The spoke stand-in was the existing exe.dev VM, configured as a
`static_spokes` entry with `forward: tcp`.

| # | Question | Result |
|---|---|---|
| S1 | Does `HERDR_AGENT=<harness>` on the pane's foreground process give screen detection through ssh? | **Yes, but only if the hinted process is the wrapper.** On macOS herdr cannot read the environment of Apple platform binaries: `HERDR_AGENT=pi /usr/bin/ssh …` and `HERDR_AGENT=pi /bin/sleep` were both ignored, while a Homebrew python3 with the hint was detected. On Linux, `/proc/<pid>/environ` is readable. So the wrapper (python) carries the hint and re-execs itself once so the hint is in its exec-time environment. |
| S2 | Are reports relayed through a reverse-forwarded socket accepted, including full-lifecycle authority and `resume_argv`? | **Yes.** pi's own integration took authority from the spoke (`screen_detection_skip_reason: full_lifecycle_hook_authority`), and states and done worked. `resume_argv` on `herdr:pi` was accepted. After `herdr session stop` and a restart, herdr ran `spoke attach dev main --harness pi` in the restored pane and the agent, still alive in dtach, was reattached. `herdr:claude` with `resume_argv` was rejected (`resume_not_accepted`), as the source predicts, so claude and codex session refs are stripped and `hubd` reattaches those panes by cwd. |
| S3 | Which methods do spoke-side clients use? | The herdr CLI starts with `ping`, then `pane.report_metadata`, `plugin.action.invoke` (`attention-queue.refresh`) and `notification.show`. pi's integration uses `pane.report_agent` and `pane.report_agent_session`. Everything else (`pane.list`, …) is denied, and the denials are logged. |
| S0a | Does exe.dev's sshd do `-R` unix-socket forwarding? | **No.** The socket is created, but connecting to it gives `EACCES`. TCP `-R 127.0.0.1:port:/local.sock` works. This only matters when an exe VM is the *spoke* (`forward: tcp`). machine0 spokes run OpenSSH, and the hub only dials out. |
| S6a | Does the spoke pi extension broker `openai-codex` and `radius`? | **Yes.** In a temp agent dir holding an expired access token and the sentinel refresh token, `pi -p` with `openai-codex/gpt-6.1-sol` asked the relay for a credential and answered "ok". `auth.json` kept the sentinel. Radius authenticated the same way; the request then failed with 402 because the Radius balance is $0. |
| S6b | Does `broker.mjs` load the pi SDK and return credentials? | **Yes** (`get`, `status`), against a copy of a credential, with a minimum validity short enough that it did not refresh. |
| e2e | Wrapper → ssh → `spoke run` → dtach → pi, then a reconnect | pi ran on the spoke. The pane showed pi with its session and the `spoke=dev/main` token, and a prompt completed. Killing the ssh client made the wrapper reconnect and reattach the same dtach session. `spoke open-slot` on the spoke opened a new hub pane through the relay. |

Found and fixed during the spike: with no session, a new slot started `pi -c`, which continues the most recent session for the directory and so took over another slot's session. A slot without a session now starts a fresh `pi`.

Still open, because they need the real hub, a machine0 account and logins: S0 contention on the exe pool, S4 (machine0 suspend/resume, IP and host-key changes, images), S5 (setup-token in pi and Claude Code), S6 on the hub's own broker logins (refresh margin), S7 (usage), S8 (herdr's image paste path on Linux).
