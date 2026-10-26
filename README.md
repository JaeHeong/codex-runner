# Codex Runner

Resume an existing local Codex session after its ChatGPT subscription quota resets.
The CLI registers sessions in a private per-user queue; a per-user background service
manages multiple sessions independently, with up to four concurrent workers by
default. No model call is made while waiting.

Version: **0.8.1**. Tested with **Codex CLI 0.163.0**. Linux packages require
Python 3.10+ and APT 2.4+ for inline-key repository configuration. The Debian
package is architecture independent; the repository advertises amd64 and arm64.
Ubuntu 22.04+ and Debian 12+ meet these runtime requirements. Windows and macOS
native downloads bundle Python and are specific to the listed CPU architecture.
Other distributions/CLI versions need their own validation. This is an independent community tool, not an OpenAI product.

## Install

Linux APT installation on the server where your Codex history and project files live
(macOS and Windows installation is described below):

```sh
curl -fsSLo /tmp/codex-runner.sources https://apt.jaehyeong.com/codex-runner.sources
sudo install -m 0644 /tmp/codex-runner.sources /etc/apt/sources.list.d/codex-runner.sources
sudo apt update
sudo apt install codex-runner
```

The `.sources` file embeds this repository's public signing key with `Signed-By`;
it does not add a global APT trust key. The detached public key and fingerprint are
also published at https://apt.jaehyeong.com/ . The APT repository verifies the
package using a signed `InRelease` file and package checksums.

Install and log into Codex CLI separately as your regular user. The package
contains no OpenAI credentials, API keys or bundled Codex binary. Installing it
does not start a daemon or register any session.

## Use

Copy the UUID from `codex resume <UUID>`. The default execution mode uses a separate
CLI process: finish/close that session's interactive client before handing it over.
For a terminal connected to the local managed Codex server, use `--managed` below.

```sh
# Validates CLI capabilities, ChatGPT login and quota RPC without a model call.
codex-runner doctor

# Register exactly one existing session and its actual working directory.
codex-runner add YOUR-SESSION-UUID --cwd "$HOME/my-project"

systemctl --user daemon-reload
systemctl --user enable --now codex-runner.service

codex-runner status
codex-runner logs YOUR-SESSION-UUID --follow
codex-runner pause YOUR-SESSION-UUID
codex-runner resume YOUR-SESSION-UUID
codex-runner remove YOUR-SESSION-UUID
```

### Short names and a clickable terminal menu

Version 0.4 removes the need to keep copying UUIDs. Give an existing registration
a short name, or provide `add --name tech` when registering a new one. New
registrations otherwise default to their project directory name; duplicate
directory names get a unique ID suffix. Renaming does not start or stop work.

```sh
codex-runner alias YOUR-SESSION-UUID tech
codex-runner resume tech
codex-runner pause tech
codex-runner logs tech --follow
codex-runner prompt tech --text '이전에 하던 작업 이어서 해줘'
```

Names are case-insensitive and unique within the runner state directory. Full
UUIDs and unique UUID prefixes of at least six characters remain accepted. A
prefix matching multiple sessions, or conflicting with another session's name,
is rejected rather than choosing an arbitrary session. `status` shows names and
short IDs; `status --json` keeps the full ID and all metadata.

```sh
codex-runner              # Same as codex-runner menu (or ui)
codex-runner logs -f      # Infer the session from the current project directory
codex-runner resume       # Infer a unique project; otherwise show the picker
```

The menu refreshes the session list and current state. Use Up/Down or click a row
to select it. Click a bottom button, or press A (add), D (unregister),
L (logs), R (resume), P (pause),
N (rename the selected session), or E (edit the next continuation message).
For example, `codex-runner alias tech platform` renames `tech` from the CLI.
Names must be unique; blank input in the menu cancels. Renaming preserves the
selected session even if its new name changes its sort position.
Left/Right or Tab select a button; Enter
activates it. Double-clicking a row opens logs. Q closes the menu. Merely opening,
selecting a row, or quitting does not resume work or cancel a running task.
Ctrl+C in the menu's live log view returns to the menu without stopping Codex.

The rightmost `PROMPT` column shows each session's current continuation message.
It receives the remaining terminal width; other columns stay bounded. Long or
multiline messages are shown on one line with an ellipsis. Widening the terminal
reveals more text; E / Prompt displays and edits the complete stored message.

The menu needs an interactive terminal, Python's standard `curses` module, and
at least 64 columns by 20 rows. Mouse use depends on terminal forwarding; arrow
keys work without a mouse. It does not change tmux options. Mouse clicks were
also tested through an isolated tmux server with `mouse=off`.

For `logs`, `pause`, `resume`, `remove`, and `prompt`, the session argument is
optional. The most specific current project directory selects its unique session;
if only one session is registered it can also be selected automatically. Otherwise
interactive commands show a picker, and noninteractive commands require a name or
ID. No UUID is guessed in an ambiguous situation. `add` still requires the real
existing Codex session UUID on the command line; the menu can find it from history.

### Add and remove from the menu

Version 0.5 adds a searchable browser like `codex resume`: press A / Add, choose a
project folder, then choose a session by its conversation name and most recent
activity time. Projects are ordered by recent activity and show their session
counts. Type to filter either list, use arrows or click to select, and press Enter
or double-click to choose. Esc cancels. A manual folder-path option is also
available. Only existing local, nonarchived Codex sessions are listed; create a
new conversation in Codex first if the project has no history.

Choose the execution permissions and a short Runner name next. The menu registers
the selected session in managed mode, **paused**. It does not send a continuation
message until you explicitly press R / Resume. Selecting an already registered
session focuses that registration without changing it. The command-line `add`
continues to queue work immediately as documented above.

D / Delete asks before removing the Runner registration. It preserves the original
Codex conversation, project files and saved Runner logs. Running sessions must be
paused first. Removing a registration stops future Runner continuations for it.

### Account limits in the menu

The lower-right panel shows the selected registration's authenticated Codex
account limits, with remaining-capacity bars and local reset times. It refreshes
in the background every 30 seconds while the menu is open; F5 requests a refresh.
With no registrations, it uses the current shell's Codex executable and CODEX_HOME.
These are metadata requests only: opening the menu does not start a model turn,
resume work, or change the daemon's existing wait timer.
The session table shows `checking` while managed preflight/quota checks run;
`running` identifies a turn the Runner actually owns.

The Context window line uses the selected session's latest recorded token sample,
not the sum of tokens across its history. It updates when Codex writes a new
sample. The percentage is marked `~` because the native Codex UI may reserve
additional context space; the displayed used/window token counts are the recorded
values. Missing samples show N/A. Recent conversation time and title also appear
below the session table. Failed quota refreshes keep the last successful value
with its original timestamp and a cached/error label.

### Multiple sessions

From version 0.6, a long-running session does not monopolize the daemon. Each
registered session has an independent worker, state, continuation message, retry
time and log stream. Pause, completion or an error affects that registration only.
Each worker opens its own SQLite connection. An atomic claim and an active-session
registry prevent the same registration from being dispatched twice.

The default limit is **four concurrent workers**, including workers tracking owned
managed turns. Additional ready sessions remain `queued`; their detail explains
that they are waiting for a worker slot. Quota waits and checks of another active
interactive turn return their worker slot promptly and are retried at their own
scheduled time. Fair ordering uses the least recently checked session first.

For foreground operation, configure the limit directly:

```sh
codex-runner daemon --max-parallel 8
```

For the existing user service, add this override with
`systemctl --user edit codex-runner.service`, then reload and restart that service:

```ini
[Service]
Environment=CODEX_RUNNER_MAX_PARALLEL=8
```

The value must be at least 1; setting it to 1 restores sequential processing.
`daemon --once` remains a diagnostic command that handles at most one ready session.
The daemon remains a singleton per state directory. On a graceful Runner restart,
all workers stop together; CLI children are stopped and left queued, while managed
turn IDs are preserved for reattachment without sending the message again.

Matching executable/CODEX_HOME/quota-bucket workers share a synchronized 20-second
quota snapshot, including one in-flight lookup per key. Separate runtime/account
keys have separate locks. Confirmed exhaustion makes the affected registrations
wait; multiple sessions do not create additional account allowance. Background
checks use metadata and do not consume model turns.

Public source and native CI: https://github.com/JaeHeong/codex-runner .
The GitOps repository keeps infrastructure and the archive publisher private.

### macOS and Windows

Shared downloads and installation guides: https://packages.jaehyeong.com/ .
Existing https://apt.jaehyeong.com/ clients keep working.

macOS (Apple Silicon and Intel):

```sh
brew install JaeHeong/tap/codex-runner
codex-runner service install
codex-runner
```

Native macOS builds target macOS 14+ on Apple Silicon and macOS 15+ on Intel.

Windows x64: download the Windows ZIP from the archive, extract it to a permanent
user directory, and run `codex-runner.exe service install` from that directory.
The ZIP includes Python and the terminal UI dependency. Codex CLI and its ChatGPT
login are separate prerequisites. The `/winget/` guide also provides a PowerShell
installer that verifies the catalog hash, adds the user PATH and registers the
background service. A WinGet manifest is provided under `/winget/`;
public `winget install` availability depends on the community repository accepting
it. The archive URL is a download host, not a WinGet source server.

Since 0.8.1, Windows scheduled tasks use the bundled
`codex-runner-background.exe` GUI launcher. Login-time Runner/Discord services and
their metadata/proxy children allocate no console; the normal `codex-runner`
command still opens the terminal menu. Keep both executables together; the lone
`.exe` download supports interactive CLI use, while service installation requires
the complete ZIP (or a Python installation with `pythonw.exe`).

To upgrade a previous console-based installation, close the Runner menu and rerun:

```powershell
Invoke-WebRequest https://packages.jaehyeong.com/winget/install.ps1 -OutFile install-codex-runner.ps1
powershell -ExecutionPolicy Bypass -File .\install-codex-runner.ps1
```

The installer gracefully stops installed Runner/Discord workers, waits for their
files to unlock, updates both files and refreshes existing task definitions.
`-NoService` leaves workers stopped and refreshes only tasks already registered;
a fresh file-only installation creates no task. `service install --no-start`
registers the login task without starting it immediately. Login, rather than
machine boot before login, remains the automatic startup trigger.

`codex-runner service install|start|stop|status|uninstall` uses a per-user systemd
unit on Linux, a LaunchAgent on macOS, and an interactive-user scheduled task on
Windows. Install also starts the service. macOS and Windows services start after
login and require that user's login session; sleeping/offline machines cannot run
Codex. Use a normal, non-elevated Windows terminal. Service stop/uninstall leaves
Codex history, registrations and the shared daemon intact. Avoid installing a
second service for the same state directory.

Before a package upgrade, stop Runner with `codex-runner service stop`; start
it afterward with `codex-runner service start`. Shared managed Codex work stays
in its separate daemon. The Windows installer performs this stop before copying
updated files.

Default state directories are `~/.local/state/codex-runner` (Linux),
`~/Library/Application Support/CodexRunner` (macOS), and
`%LOCALAPPDATA%\CodexRunner` (Windows). `--state-dir` or
`CODEX_RUNNER_STATE_DIR` overrides them. POSIX directories use mode 0700; Windows
uses the user's inherited AppData permissions. Keep custom state paths private.

Managed sessions must be on the same machine, user account and `CODEX_HOME` as
the desktop/CLI session. Windows-native and WSL histories are separate. Windows
uses the official `codex app-server proxy` to connect to the shared local socket;
macOS/Linux connect directly. If a Windows launcher prevents detached processes,
Runner waits for the shared daemon instead of becoming a permanent error. Open
Codex desktop/CLI or run `codex app-server daemon start` in a normal terminal.
The Windows CI checks native socket IPC as a standard user using a test-owned
foreground server, since GitHub’s outer job prevents detached descendants. Neither connection starts a duplicate session writer.

### Discord remote controls and questions

Version 0.8 adds an optional bot that runs as the same local user as Runner.
It uses the Discord gateway, so no public inbound port or interaction web server
is needed. The bot token and channel configuration stay in private local state,
never in Git, a package, a CI secret, an Argo CD manifest or a model prompt.

Linux APT users install the optional companion (the normal queue stays stdlib-only):

```sh
sudo apt update
sudo apt install codex-runner codex-runner-discord
codex-runner discord configure --guild-id YOUR_SERVER_ID --channel-id YOUR_CHANNEL_ID --allow-all-users
codex-runner discord token
codex-runner discord invite
codex-runner discord doctor
codex-runner discord service install
```

`token` prompts without echo and writes `discord.token` with POSIX mode 0600.
`invite` prints a server-specific invitation with `bot` and `applications.commands`
scopes. Approve the invite from your Discord account. Grant View Channel, Send
Messages, Embed Links and Read Message History; channel members also need Use
Application Commands. Message Content, Members and Presence privileged intents
are not used. `--allow-all-users` means everyone in that exact server/channel can
control registered work with its saved permissions. To limit operators instead,
repeat `--allowed-user USER_ID` and omit that flag. DMs and all other channels
are rejected, including persistent buttons, modals and autocomplete requests.

Native macOS/Windows downloads include the optional bot libraries. Python users
install `codex-runner[discord]`; Debian's companion bundles pinned discord.py and
uses distro aiohttp. On Python 3.13+ the distro must also provide audioop-lts;
the Python extra resolves that dependency when using a virtual environment.

Commands in the configured channel:

| Command | Behavior |
| --- | --- |
| `/runner status [session]` | Status and queued input; session names autocomplete |
| `/runner limits [session]` | Five-hour and weekly account limits; no model call |
| `/runner resume session` | Re-arm the normal completion/blocker checks |
| `/runner pause session` | Pause Runner; it interrupts only a Runner-owned turn |
| `/runner send session message` | Queue a new one-time instruction, including a reply to a blocker |
| `/runner questions session` | Open pending questions and answer controls |

Notifications cover blockers, errors, completed/idle responses, pauses and quota
waits. Repeated metadata polls do not repeat the same notification. Buttons offer
status, limits, resume, pause, a new-instruction modal and question answers. Bot
restarts restore the persistent controls and replay live native questions.
Messages sent as commands use durable interaction IDs: delivery retries cannot
create a second queued instruction. A network failure after an uncertain model
start stops for inspection instead of replaying the request.

A plain Resume preserves the existing blocker guard. Use **추가 지시** or `/runner
send` to provide missing information, or explicitly instruct Codex which remaining
work to continue. Queued instructions wait for the current native turn and linked
agents to stop, then wait for quota availability. They use the registration's saved
sandbox, model and approval policy. They do not alter the permanent continuation
prompt. The equivalent server command is:

```sh
codex-runner send tech --text '검증은 보류하고 진행 가능한 나머지 작업을 이어서 해줘'
```

Once connected, the bot subscribes to registered managed sessions and verified
native child agents. A live `requestUserInput` is kept intact and shown with answer
controls. Replies go to the existing RPC request; they do not create another turn.
A terminal answer or expired turn invalidates the buttons. Secret questions and
command/file/permission approval requests require the native Codex terminal and are never accepted
automatically. After relay connection, transient Discord outages keep native
questions intact for reconnection or a terminal answer. The menu shows `input`
for a pending blocking question; the underlying worker keeps its turn ownership.

The Discord service is separate from Runner:

```sh
codex-runner discord service status
codex-runner discord service stop
codex-runner discord service start
codex-runner discord service uninstall
```

Stopping the bridge leaves native turns/questions intact. Uninstall disables the
relay policy for future requests. Its logs are private (`discord-service.log` on
macOS/Windows; the user journal on Linux). No normal channel chat is treated as
an instruction. Model work happens only through an explicit command or the
existing quota recovery cycle. A complete task remains stopped without a new
instruction.

### Idle, waiting and completion

Version 0.6.1 uses `idle` for an ordinary finished Codex response. It sends no new
message and keeps observing the registered managed session every 15 seconds.
`waiting` means there is an active turn, linked agent, or quota restriction to wait
for. A later native task is observed without interruption; after a confirmed quota
failure (`usageLimitExceeded` or an explicit supported workspace-budget code),
the normal quota recovery checks can hand it to Runner. Esc interruptions and
ordinary native errors remain idle without an automatic retry. Explicit Resume
can still authorize a retry, and Pause disables observation.

`complete` is different: Codex explicitly reported that the Runner-owned task is
finished, so that automatic execution cycle stops. Resume is needed to hand over
another task. `blocked` remains for actual input requirements or runtime/history
states that need inspection. Updating reclassifies only the old ordinary-response
guard from `blocked` to `idle`; genuine blockers and paused registrations stay put.

Version 0.6.2 also handles Codex's `systemError` runtime after a confirmed latest
quota failure. It waits for account availability and resumes once, checking again
for terminal activity before sending anything. Other system errors still stop for
inspection. Upgrading rechecks registrations stopped by the old `systemError`
guard in observation mode: active work is left running, and a finished ordinary
response becomes idle without another message. Paused sessions, uncertain starts,
owned turns, and unrelated errors are preserved.

### Readable activity logs

Version 0.3 makes `logs` a readable activity view by default. It shows Codex's
visible explanations, commands, command output, exit status, file changes, plans,
errors, and completion summaries. JSON envelopes, token counters, settings events,
and internal reasoning are hidden. Streaming text chunks are joined, and completed
messages/output are not printed again after their deltas.

```sh
codex-runner logs YOUR-SESSION-UUID --follow
codex-runner logs YOUR-SESSION-UUID --lines 100
codex-runner logs YOUR-SESSION-UUID --follow --full
codex-runner logs YOUR-SESSION-UUID --local
codex-runner logs YOUR-SESSION-UUID --raw --follow
```

In managed mode, the default view reads recent conversation items directly from
the same local Codex server. `--follow` subscribes to its activity, including an
existing tmux turn while the runner waits to take over. This observer does not
send instructions, call a model, approve requests, or interrupt work. Closing it
with Ctrl+C stops observation only. Questions and approval waits are identified;
answer those in the actual Codex terminal, not in this read-only log view.

The default displays the latest 60 readable lines from recent activity. Long
commands and tool output are compacted; the last diagnostic lines are retained.
`--full` removes those preview limits. A snapshot item already in progress when
you attach is reconciled at completion to avoid duplicate text; new items stream
normally. Markdown stays plain text; this is an activity viewer, not the full
interactive Codex TUI.

`--local` formats saved runner events without connecting to Codex. An unavailable
managed server falls back to saved events. `--raw` retains the original saved
JSON/stderr view for debugging and never connects to the live server; activity
that ran outside the runner may only be present in Codex's own conversation.
`--lines` selects readable lines by default and lines per file in raw mode.

### Change the continuation message

The built-in default from version 0.3.1 is exactly:

```text
이전에 하던 작업 이어서 해줘
```

Completion status and verification requirements remain in the structured result
schema, separately from this short user-facing message. Existing registrations
keep their stored message until explicitly changed:

```sh
codex-runner prompt YOUR-SESSION-UUID
codex-runner prompt YOUR-SESSION-UUID --default
codex-runner prompt YOUR-SESSION-UUID --text '이전에 하던 작업 이어서 해줘'
codex-runner prompt YOUR-SESSION-UUID --file "$HOME/runner-task.txt"
```

`prompt` without an option prints the current message. The setters replace the
entire stored message and apply to the next continuation turn. They do not send
anything to an active turn, reset a quota timer, unpause a job, retry an error,
or restart the daemon. No service restart is needed to change a registered message.
The `add --prompt-file` option still appends extra instructions to the default;
`prompt --file` replaces the whole message. Files are read when the command runs.

### A session already open in tmux (managed server)

Version 0.2 adds an opt-in backend for Codex's existing local app-server. The same
conversation can remain open in a terminal for observation; no second CLI session
writer is started. Let any current turn finish before handing over. If another
turn is active when the runner tries to start, it waits and checks again every
15 seconds without interrupting or duplicating that turn. Once the existing turn
finishes, the runner checks quota and can take over the unfinished work.

```sh
# A private text file containing the actual authorized task/instructions.
codex-runner add YOUR-SESSION-UUID --cwd "$HOME/my-project" \
  --managed --prompt-file "$HOME/runner-task.txt"
```

An existing registration must first be paused, removed, and re-added to change its
mode. The default sandbox remains `workspace-write`; `--sandbox danger-full-access`
explicitly enables Full Access. The task file is read at registration, not watched
for later edits. Its contents are appended to the normal continuation instructions.

The terminal may be detached or closed while managed work runs. Reconnect with
`codex resume UUID` to observe the same conversation, or use `codex-runner status`
and `codex-runner logs UUID --follow`. Do not send new terminal instructions while
the runner owns an unfinished job; pause it and confirm `paused` first. Once the
registration is `complete`, `blocked`, or `paused`, it does not watch for new
terminal messages: explicitly hand over the next task with `resume` or re-register
with an updated task file.

The runner records the exact owned turn ID. Service restarts reattach to that turn
without sending the task again; stopping the runner service alone leaves managed
work running on the Codex server. Use `pause` to cancel it. A pause is confirmed
only after that turn has stopped. A missing start response stops for inspection
instead of replaying a potentially accepted request. Reboot recovery can start the
local Codex daemon again. Authentication/transport failures still stop for review.
This backend uses the version-specific experimental local app-server protocol.

From version 0.5.1, managed Resume checks runtime state and the latest turn before
queuing, and the worker checks again after quota lookup, immediately before sending
the continuation message. An active turn (including approval/input waits) is left
alone. A structured `complete` result stops without another message; `continue`
can proceed, and `blocked` stops for review. A normal completed Codex turn only
means that response ended: it does not prove the entire task finished. From 0.6.1,
such a turn stays `idle` and is observed without sending another message. To
deliberately send the continuation message after a completed response, use:

```sh
codex-runner resume tech --continue-completed
```

That override applies to the exact checked turn, so a later completed terminal
turn cannot inherit permission to continue. Interrupted/failed turns can resume
under the existing quota rules. Missing or inconsistent runtime/history information
stops for inspection instead of inventing an idle/busy state. Resume checks do not
make a model request. Default CLI mode only guards the Runner's stored `complete`
state; live terminal checks require managed mode.

The latest turn's agent references are followed through verified `parentThreadId`
relationships. A live active child or descendant keeps the parent waiting, even
when the parent is idle. Old saved `running` agent snapshots alone are not trusted;
current runtime/history is checked. Only the linked task tree is examined (up to
32 threads), not every agent or process on the server. An unfinished saved turn
without a confirmed active runtime stops for inspection. Persistent shell commands,
development servers, log followers and monitoring processes are not automatically
treated as unfinished work. Their command names are not used to guess intent.

These checks reduce races but cannot provide an atomic lock against terminal input:
the tested Codex `turn/start` schema has no start-only-if-idle precondition, and
can add input to an already active turn. Keep terminal input paused while Runner
owns the registration. Updating the package does not restart the running user
service: new worker checks take effect when that service next starts. New menu/CLI
commands use the updated preflight immediately.

`pause` requests cancellation and waits asynchronously for the current child to
stop (normally within a few seconds). Watch `status` until it says `paused` before
reopening that session interactively. `remove` unregisters it after cancellation;
it preserves Codex history, project files and existing private logs.

For an nvm or custom Codex installation, `add` captures the resolved Codex executable
location, PATH and CODEX_HOME so the user service can find the same runtime without
an interactive shell. Use `--codex /absolute/path/to/codex` to override discovery.
Re-register with the new executable path if an nvm version is removed.

For operation after logout and before the first login following a reboot, enable
linger for your user (requires administrator privileges):

```sh
sudo loginctl enable-linger "$USER"
```

Otherwise, the enabled service starts with the user's systemd login manager.
To stop all runner activity, use `systemctl --user stop codex-runner.service`.
Registered unfinished jobs remain queued for a subsequent service start.

Foreground operation without systemd is also supported:

```sh
codex-runner daemon
# Or, inside tmux:
tmux new -s codex-runner 'codex-runner daemon'
```

## Execution and quota behavior

- The exact session ID is passed to `codex exec resume`, or to the local app-server
  in `--managed` mode; `--last` is never used.
- `account/rateLimits/read` provides the actual reset times. Exhausted short-term
  and weekly windows are evaluated together; the later applicable reset wins.
  The runner checks again 45 seconds after that reset, rather than sleeping a
  fixed five hours or assuming capacity has recovered.
- A single daemon dispatches independent session workers, with a configurable
  concurrency limit (four by default). Quota snapshots are synchronized across
  matching executable/CODEX_HOME/bucket jobs and briefly cached. Other CLI/app usage
  can still consume the same account allowance; capacity is refreshed after a failed run.
- The default metered bucket is `codex`. `codex-runner limits` displays it;
  `add --limit-id ID` selects another known bucket when the chosen model uses one.
  Unknown buckets stop the job instead of guessing.
- A `--model` override is optional. Without it, Codex's normal session/config model
  selection applies. The runner never switches models or accounts to evade limits,
  consumes earned reset credits, or automatically purchases credits.
- API-key authentication does not substitute for a missing ChatGPT login.
- Version 0.2.1 fixes a missed-resume bug: workspace credit/usage limits previously
  became permanent `error` registrations, even if the allowance recovered later.
  Known owner/member credit or usage-limit states and spend controls now remain
  `waiting`. The runner polls account metadata every five minutes without a model
  call; it never buys credits, changes accounts, or assumes credits reset on a
  five-hour schedule. An earlier reported exhausted-window reset is checked first.
  Once the server clears the restriction and quota is available, work resumes.
  Unknown reset times are polled too; unrecognized limit-state values stop for review.
- In managed mode, a structured `usageLimitExceeded` turn failure also schedules a
  five-minute recheck if the quota endpoint has not yet reflected the restriction.
- Quota RPC, authentication, configuration and ordinary execution failures become
  `error`, preserving logs; resolve the issue and explicitly `resume` the registration.
  Confirmed subscription, workspace, and structured usage-limit failures wait as
  described above; ordinary execution errors still stop for review.
- Codex returns a schema-validated result: `complete`, `continue` or `blocked`.
  Successful turns are continued only when work remains. Verified completion,
  a user-input blocker or three identical continuation summaries stop the loop.
  Completion is the model's assessment against the original session instructions;
  use concrete acceptance criteria in that original task.
- State is durable SQLite. Duplicate daemon instances are locked out, pause requests
  cannot be overwritten by a finishing child, and interrupted jobs recover on
  daemon restart. If a surviving old process is detected, the registration stops
  for review rather than launching a duplicate.

## Permissions and data

Run the runner as your existing Codex login user, not root. Default execution uses
`workspace-write` and approval policy `never`, so unattended work cannot wait on a
terminal approval. Operations requiring additional permission can fail and must
be reviewed through the normal workflow. `add --sandbox read-only` is available
for review work; `danger-full-access` is an explicit opt-in for environments where
that authority is intended. There is no default sandbox bypass.

In default CLI mode, do not attach an interactive client while the runner owns the
session. In `--managed` mode, an interactive client may observe it but should not
send new instructions until the runner is paused or finished.
Session IDs alone do not transfer history to another server: use the same local
history/CODEX_HOME and the corresponding project checkout. Changes to project files
can occur while a session waits, so every continuation first inspects current state.

State and logs live under `$XDG_STATE_HOME/codex-runner` (normally
`~/.local/state/codex-runner`), with a private directory and umask 0077. Each session
keeps the latest 20 attempt log/output files of each type. Logs include Codex tool
output and may contain sensitive project data; they remain on the local machine.
`--state-dir` overrides the location. No task history or credentials are sent to
apt.jaehyeong.com; that hostname only distributes static public files.

## Updates and removal

```sh
sudo apt update
sudo apt install --only-upgrade codex-runner
systemctl --user restart codex-runner.service

# Stop the user service before uninstalling:
systemctl --user disable --now codex-runner.service
sudo apt remove codex-runner
```

Package removal leaves your private state and Codex sessions intact.

## Development and releases

Source archive: https://packages.jaehyeong.com/projects/codex-runner/0.8.1/codex-runner-0.8.1.tar.gz

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m codex_runner.cli --version
```

The tests use a fake CLI and no paid model requests. They cover quota reset timing,
weekly limits, mid-run exhaustion and recovery, non-quota failures, wrong session
IDs, completion/continuation, cancellation, singleton locking and crash recovery.
Managed-mode tests also cover active interactive turns, quota recovery, reattaching
owned turns, ambiguous start failures, confirmed cancellation, and partial frames.
Preflight tests cover completed tasks, ambiguous native final answers, turn-scoped
overrides, state changes during quota lookup, inconsistent saved/runtime states,
linked running agents, stale agent snapshots, and persistent background shells.
Scheduler tests cover overlapping sessions, bounded concurrency, independent pause
and errors, single-claim dispatch, shared quota exhaustion/recovery, separate-account
lookups, stopping CLI children, and managed turn reattachment without write RPCs.
Log-view tests cover Unicode streaming, duplicate suppression, native CLI events,
readable final results, compact/full output, input waits, partial JSON records,
attempt rotation, read-only live observation, and offline fallback.
Name/menu tests cover exact resolution, ambiguous prefixes, alias collisions,
state-preserving renames, project selection, existing commands with short names,
mouse hit targets, Unicode labels, prompt column sizing, and opening/quitting a
real terminal menu without starting work. They also cover preserving selection
after a rename, registering a browsed session paused, unregister confirmation,
paginated read-only session metadata, offline history fallback, context samples,
background quota refresh, cache freshness, and separate runtime/account caches.

`scripts/build_release.py` builds the `.deb` with root-normalized ownership and a
source archive. Bump the version before building a new release; published release
files are immutable. Register the artifacts and publish them with the shared
[JaeHeong/my-packages-repository](https://github.com/JaeHeong/my-packages-repository)
publisher (`python3 archive.py` from its checkout, or `jaehyeong-archive` on the
current publisher host). Its README explains registering other applications and
transferring built artifacts to the publisher; Git push runs checks separately
from the signing/publication step.
It generates all projects' APT indexes and GPG signatures together, so releasing
Codex Runner cannot overwrite another project's repository metadata or website.
APT compatibility depends on the actual Python/Codex requirements, not merely the
package's `all` architecture.

The archive origin is one small static Nginx replica in namespace `jaehyeong`,
managed by Argo CD. Public files live on a Retain PVC, with atomic publication and
three retained deployment snapshots. All project versions remain in the current
archive. The signing key, Codex credentials and local queue state are not stored
in the public archive or in Kubernetes ConfigMaps. Existing APT setup and original
download URLs remain supported. Browse https://apt.jaehyeong.com/projects/codex-runner/ .

References:
- https://learn.chatgpt.com/docs/non-interactive-mode
- https://learn.chatgpt.com/docs/app-server
- https://manpages.debian.org/apt-secure
- https://manpages.debian.org/sources.list
