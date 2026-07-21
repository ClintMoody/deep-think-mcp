# Wiring deep-think-mcp into an MCP client

> **Long-lived agent host (Hermes-style gateway, or anything that caches a
> session's tool schema)?** Do **not** use the stdio snippets below — stdio
> loses the startup race in those hosts and the tools silently vanish from
> sessions. Run the always-live HTTP daemon and point the host at its URL
> instead: see [`http-transport.md`](http-transport.md). That doc also covers
> verification, orphan cleanup, and full uninstall steps for every client.

deep-think-mcp is a dev-checkout, stdio-transport MCP server: every client
below launches it the same way,

```
uv --directory /absolute/path/to/deep-think-mcp run python -m deep_think_mcp.server
```

Replace `/absolute/path/to/deep-think-mcp` with wherever you cloned this
repo (see `README.md` § Install — clone with `--recurse-submodules`, or run
`git submodule update --init` afterward, then `uv sync`). This form is
**portable**: it doesn't depend on your shell's current working directory
or on activating a virtualenv, because `uv --directory <path>` resolves the
project from `<path>` regardless of where the MCP client itself runs the
command from.

Every snippet below also sets `DEEP_THINK_HOME` explicitly. It's optional
(the server defaults to `~/deep-think-mcp/` if unset), but setting it
avoids ambiguity when several MCP clients on the same machine might
otherwise share one data root.

## Claude Desktop

Edit `claude_desktop_config.json`:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- Linux: `~/.config/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "deep-think-mcp": {
      "command": "uv",
      "args": [
        "--directory", "/absolute/path/to/deep-think-mcp",
        "run", "python", "-m", "deep_think_mcp.server"
      ],
      "env": {
        "DEEP_THINK_HOME": "/absolute/path/to/deep-think-mcp-data"
      }
    }
  }
}
```

Restart Claude Desktop for the change to take effect.

## Claude Code

Either the CLI (creates a `local`- or `project`-scoped entry) or a
project-level `.mcp.json` file work.

**CLI** (`--` separates `claude mcp add`'s own flags from the server
command; `--env` may be repeated for more variables; `--scope` is
`local` (default, just this machine), `project` (checked into
`.mcp.json`, shared with the team, prompts for approval on first use), or
`user` (available across all your projects)):

```bash
claude mcp add --env DEEP_THINK_HOME=/absolute/path/to/deep-think-mcp-data \
  --scope project deep-think-mcp \
  -- uv --directory /absolute/path/to/deep-think-mcp run python -m deep_think_mcp.server
```

**`.mcp.json`** (repo root of the project you want deep-think-mcp available
in — this is what the CLI command above actually writes with
`--scope project`):

```json
{
  "mcpServers": {
    "deep-think-mcp": {
      "type": "stdio",
      "command": "uv",
      "args": [
        "--directory", "/absolute/path/to/deep-think-mcp",
        "run", "python", "-m", "deep_think_mcp.server"
      ],
      "env": {
        "DEEP_THINK_HOME": "/absolute/path/to/deep-think-mcp-data"
      }
    }
  }
}
```

## Cursor

Edit `.cursor/mcp.json` (project-scoped, repo root) or `~/.cursor/mcp.json`
(global, available in every project):

```json
{
  "mcpServers": {
    "deep-think-mcp": {
      "command": "uv",
      "args": [
        "--directory", "/absolute/path/to/deep-think-mcp",
        "run", "python", "-m", "deep_think_mcp.server"
      ],
      "env": {
        "DEEP_THINK_HOME": "/absolute/path/to/deep-think-mcp-data"
      }
    }
  }
}
```

## Continue

Continue's `mcpServers` block is a **list**, not an object keyed by name,
and each entry supports a `cwd` field as an alternative to `--directory`.
Add this to `~/.continue/config.yaml` (or your assistant's block in a
Continue Hub config):

```yaml
mcpServers:
  - name: deep-think-mcp
    command: uv
    args:
      - --directory
      - /absolute/path/to/deep-think-mcp
      - run
      - python
      - -m
      - deep_think_mcp.server
    env:
      DEEP_THINK_HOME: /absolute/path/to/deep-think-mcp-data
```

## LibreChat

Add a `mcpServers` block to your `librechat.yaml` (top-level, alongside
`version`/`cache`/etc.). LibreChat calls the transport `type` explicitly:

```yaml
mcpServers:
  deep-think-mcp:
    type: stdio
    command: uv
    args:
      - --directory
      - /absolute/path/to/deep-think-mcp
      - run
      - python
      - -m
      - deep_think_mcp.server
    env:
      DEEP_THINK_HOME: /absolute/path/to/deep-think-mcp-data
```

Restart the LibreChat server after editing `librechat.yaml` — MCP servers
initialize at startup.

## Sanity-checking a wiring config

Any of the above should make these tools show up in the client's tool
list: `start_session`, `set_session_mode`, `list_modes`,
`resume_session`, `list_sessions`, `clear_session`, `finalize_session`,
`move_session`, `keep_here`, `advance_stage`, the six serial-mode tools
(`begin_thought` ... `commit_thought`), the four subagent-mode tools
(`begin_subagent_thought` ... `commit_subagent_thought`), and the five
meta tools (`next_action`, `summarize_session`, `compress_history`,
`export_session`, `import_session`) — 25 tools total. If you've also set
`[autopilot].enabled = true` in `<DEEP_THINK_HOME>/config.toml`, two more
(`run_stage_autopilot`, `run_subagent_autopilot`) bring that to 27.

You can verify the command itself works standalone, outside any client,
with the MCP Inspector:

```bash
npx @modelcontextprotocol/inspector uv --directory /absolute/path/to/deep-think-mcp run python -m deep_think_mcp.server
```
