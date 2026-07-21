# Running deep-think-mcp as a Streamable HTTP daemon

## TL;DR

deep-think-mcp now supports two transports from the same entrypoint:

| Transport | How it runs | Use it for |
|---|---|---|
| `stdio` (**default, unchanged**) | The client spawns one server process and owns it | A single agent that launches the server itself |
| `streamable-http` | One long-lived daemon; clients connect over a URL | **Sharing one always-live server between multiple clients** (a Hermes agent *and* a Dagu DAG), and avoiding stdio startup races |

```bash
# stdio (identical to the old behaviour — nothing to change for existing users)
python -m deep_think_mcp.server

# HTTP daemon on http://127.0.0.1:8182/mcp
python -m deep_think_mcp.server --transport streamable-http --host 127.0.0.1 --port 8182 --path /mcp
```

## Why this was added

When deep-think ran under stdio inside a long-lived agent host (Hermes), its 29
tools registered correctly but **kept disappearing from the model's tool list**.
Root cause: the host builds and *caches* a session's tool schema once, and only
includes an MCP server's tools if that server's connection is **live at that
exact instant**. A stdio server is (re)spawned per connection and takes ~0.4s+
to import and complete its MCP handshake, so it routinely loses the race against
the cached prompt and is silently dropped for the whole session — intermittently
and maddeningly.

An HTTP daemon is **always live**: the host's liveness check passes immediately,
every session, so the tools are always present. This is exactly why HTTP-based
MCP servers (in a typical Hermes setup: qmd, context7, open-brain) never exhibit
the problem, while stdio ones can.

## Is sharing one daemon safe?

Yes, within the model deep-think was built for. All session state is persisted
to disk under `config.resolve_root()` (default `~/deep-think-mcp`, overridable
via `DEEP_THINK_HOME`), keyed by `session_id`, and every write is guarded by a
`portalocker` file lock. So multiple clients can share one daemon safely **as
long as they don't drive the _same_ `session_id` truly concurrently**. deep-think
documents a single-client assumption for one non-atomic window (`set_session_mode`);
in practice that window is only reachable by concurrent calls to the same
session, which a single-inference-slot backend (e.g. llama.cpp `-np 1`) already
serializes system-wide.

If you have genuinely concurrent one-shot callers and don't need a persistent MCP
session, add `--stateless` (or `DEEP_THINK_MCP_STATELESS=1`). This only changes
the MCP *protocol* session handling; deep-think's own on-disk session state is
unaffected.

## Flags and environment variables

Every flag falls back to an env var, so a systemd unit or container can drive it
without a custom command:

| Flag | Env var | Default | Meaning |
|---|---|---|---|
| `--transport` | `DEEP_THINK_MCP_TRANSPORT` | `stdio` | `stdio` \| `streamable-http` \| `sse` |
| `--host` | `DEEP_THINK_MCP_HOST` | `127.0.0.1` | Bind host (keep local unless you mean it) |
| `--port` | `DEEP_THINK_MCP_PORT` | `8182` | Bind port |
| `--path` | `DEEP_THINK_MCP_PATH` | `/mcp` | Mount path; client URL is `http://host:port/path` |
| `--stateless` | `DEEP_THINK_MCP_STATELESS` | `false` | Stateless Streamable HTTP |
| (n/a) | `DEEP_THINK_HOME` | `~/deep-think-mcp` | On-disk session store (share across clients) |

## Run it under systemd (user service)

A ready unit ships at [`deploy/deep-think-mcp.service`](../deploy/deep-think-mcp.service),
modeled on a standard qmd-style HTTP MCP daemon:

```bash
cp deploy/deep-think-mcp.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now deep-think-mcp.service
systemctl --user status deep-think-mcp.service      # should be active (running)
```

Verify it's actually serving MCP (a bare GET is rejected by the MCP endpoint,
which *proves* it's up and routing):

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8182/mcp   # expect 400/406, not 000/connection refused
```

## Wiring a Hermes agent to the daemon

Under `mcp_servers:` in `~/.hermes/config.yaml`, the `deep-think` entry must be
a `url:` block (mirrors how qmd is configured). This is the **only** correct
shape for a Hermes wired to the daemon — install exactly this:

```yaml
  deep-think:
    url: http://localhost:8182/mcp
    timeout: 30
    connect_timeout: 15
    enabled: true
```

If the entry instead has `command: uv` / `args: [--directory, …, run, python,
-m, deep_think_mcp.server]`, that is the racy per-connection stdio form this
document exists to replace — delete it and use the `url:` block above. (This
doc deliberately does not show the stdio form as a yaml block: an automated
installer once copy-pasted the "BEFORE" example instead of the "AFTER" one.)

### Restart every process that caches the MCP config

`/reload-mcp` (or restarting the gateway alone) is **not sufficient**. Any
long-lived host process that connected while a stdio config was in effect keeps
that spec cached *in memory* and will keep respawning stdio orphans until it is
restarted — the config file on disk is only read at spawn/connect time. In a
typical full Hermes deployment that is three separate services:

```bash
systemctl --user restart hermes-gateway.service     # the gateway itself
systemctl --user restart hermes-dashboard.service   # parent of per-session slash_worker processes, which each cache the config
systemctl --user restart hermes-webui.service       # caches the MCP spec from its first connect
```

Symptom of a missed one: you kill the stdio orphans and identical ones (same
watchdog `--ppid`) reappear within seconds. Find the culprit with
`ps -o ppid= -p <watchdog-pid>` and restart that parent.

### Clean up orphaned stdio processes — without killing the daemon

Each stale stdio connection is a three-process stack: an
`mcp_stdio_watchdog.py` wrapper → `uv` → the stdio `python -m
deep_think_mcp.server`. Two traps:

- **The obvious pkill kills the daemon too.** `pkill -f 'deep_think_mcp.server'`
  matches the HTTP daemon's command line as well. The stdio processes' command
  lines *end* at `deep_think_mcp.server`, while the daemon's continues with
  `--transport streamable-http`, so anchor the pattern with `$`.
- **pkill can match your own shell.** A `pkill -f` pattern that appears
  literally inside your own compound command matches the shell running it
  (self-kill, exit code 144). Inspect first, then kill; fall back to killing by
  PID if a pattern misbehaves.

```bash
pgrep -af 'deep_think|mcp_stdio_watchdog'        # inspect first — note which PID is the daemon
pkill -f 'deep_think_mcp\.server$'               # stdio servers only ($ excludes the daemon)
pkill -f 'uv --directory.*deep-think-mcp run'    # their uv parents
# watchdogs outlive their children — kill them separately (by PID if pkill self-matches):
pkill -f 'mcp_stdio_watchdog.*deep-think-mcp' || kill <watchdog-pids>
```

### Verify the install

1. Exactly **one** deep-think process exists, and it is the daemon:
   `pgrep -af deep_think_mcp.server` → one line, containing
   `--transport streamable-http`.
2. The host registered the tools over HTTP — in the Hermes agent log
   (`~/.hermes/logs/agent.log`):
   `MCP server 'deep-think' (HTTP): registered 29 tool(s)`.
   If the line says `(stdio)`, a process is still on the old config — see the
   restart section above.
3. No respawns: re-run the `pgrep` after a couple of minutes; still one line.
   (Transient `keepalive failed, triggering reconnect` log lines *during* the
   restarts are normal churn; they should stop once everything is restarted.)

## Wiring a Dagu DAG (or any second client)

Point it at the **same** URL — `http://127.0.0.1:8182/mcp`. Because state is on
disk keyed by `session_id`, a DAG can hand a thinking session across steps (and
across processes): one step calls `start_session` and records the returned
`session_id`; later steps pass that id to `resume_session` / the serial-engine
tools. Consider `--stateless` on the daemon if your DAG steps are independent
one-shot MCP calls.

## Security posture

The HTTP daemon is designed for **local, single-operator use** (the same class
as a local qmd or llama.cpp server). Specifically:

- **Loopback by default.** It binds `127.0.0.1`, reachable only from the same
  host. Keep it there unless you have a concrete reason not to.
- **DNS-rebinding protection is on** (SDK default). The server validates the
  `Host` and `Origin` headers against a localhost allowlist, so a malicious web
  page in your browser cannot drive the daemon via rebinding — spoofed `Host`
  gets `421`, spoofed `Origin` gets `403`. Verified in the test notes below.
- **No application-layer authentication.** Any process that can reach the
  socket can call every tool. On a loopback bind that means local processes,
  which is the intended trust boundary. If you ever bind to a routable
  interface (`--host 0.0.0.0`/a LAN IP), the server logs a warning, and you
  MUST put it behind an authenticating reverse proxy **and** widen
  `allowed_hosts` — otherwise legitimate non-localhost clients are rejected by
  the rebinding guard anyway (it fails closed).
- **Concurrency / single-client caveat.** A shared daemon makes deep-think's
  one documented non-atomic window (`set_session_mode`, load-check-mutate
  across two lock acquisitions) reachable in principle. In practice it is only
  hit by two callers driving the *same* `session_id` at the same instant;
  disk writes are `portalocker`-guarded and a single-inference-slot backend
  serializes callers, so normal multi-client use (distinct sessions) is safe.
  Use `--stateless` for independent one-shot callers.

## Rollback

Set the Hermes `deep-think` block back to the `command:`/`args:` stdio form,
`systemctl --user disable --now deep-think-mcp.service`, `/reload-mcp`. No data
migration is involved — the on-disk session store is identical for both
transports.

## Uninstalling completely

Rollback (above) keeps deep-think but switches transport. This section removes
it entirely. Order matters: **clients first, then the daemon, then processes,
then (optionally) data** — removing the daemon while clients still reference it
just fills logs with reconnect noise.

1. **Unwire the clients.**
   - *Hermes:* delete the `deep-think:` block from `mcp_servers:` in
     `~/.hermes/config.yaml`, then restart every config-caching service (same
     three-service list as in the install section — the gateway alone is not
     enough).
   - *Claude Code:* `claude mcp remove deep-think` (add `--scope user` if it
     was added at user scope; check with `claude mcp list`).
   - *Anything else pointed at the URL* (a Dagu DAG, cron jobs): remove the
     reference — there is no server-side registration to undo.

2. **Remove the daemon.**

   ```bash
   systemctl --user disable --now deep-think-mcp.service
   rm ~/.config/systemd/user/deep-think-mcp.service
   systemctl --user daemon-reload
   ```

3. **Sweep leftover processes** using the anchored patterns from the cleanup
   section above (watchdogs, uv parents, stdio servers). After this,
   `pgrep -af deep_think` should return nothing.

4. **Session data (optional — this is the destructive step).** All thinking
   sessions live under `config.resolve_root()` — `~/deep-think-mcp` by default,
   or wherever `DEEP_THINK_HOME` points. Nothing else on the system references
   it, so keep it if you might reinstall, or `rm -rf` it to remove all session
   history. The user config (`config.toml`) lives inside the same root and goes
   with it.

5. **The repo clone** (e.g. `~/PROJECTS/apps/deep-think-mcp`) contains the
   code, its `.venv`, and nothing runtime-critical once the daemon is gone —
   delete it last, after confirming step 4, since the daemon unit and any
   stdio configs point into it.
