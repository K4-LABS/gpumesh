# Using gpumesh from an AI coding assistant (MCP)

`gpumesh mcp-serve` exposes your mesh as [MCP](https://modelcontextprotocol.io)
tools, so Claude Code, Cursor, or any other MCP client can list your workers
and run work on them:

> *"Run this parameter sweep across my mesh."*

It wraps the same HTTP API the CLI uses. Anything an assistant can do here
you can already do yourself with `gpumesh submit`.

## Install

```bash
pip install "gpumesh[mcp]"
```

Needs Python 3.10+. That floor comes from the MCP SDK, not from gpumesh.
gpumesh itself still runs on 3.9 and simply cannot serve MCP there.

## Configure

Nothing to configure if you have already joined or started a mesh.
`mcp-serve` reads `~/.gpumesh/config.json`, the same file `gpumesh join` and
`gpumesh serve` write. Check it with `gpumesh show-connection`.

### Claude Code

```bash
claude mcp add gpumesh -- gpumesh mcp-serve
```

Or, by hand, in `~/.claude.json` (global) or `.mcp.json` (per project):

```json
{
  "mcpServers": {
    "gpumesh": {
      "command": "gpumesh",
      "args": ["mcp-serve"]
    }
  }
}
```

### Cursor

`~/.cursor/mcp.json` (global) or `.cursor/mcp.json` (per project):

```json
{
  "mcpServers": {
    "gpumesh": {
      "command": "gpumesh",
      "args": ["mcp-serve"]
    }
  }
}
```

### Pointing at a specific mesh

To override the saved connection, say for a second mesh or a machine with no
saved config:

```json
{
  "mcpServers": {
    "gpumesh": {
      "command": "gpumesh",
      "args": ["mcp-serve", "--url", "http://192.168.1.10:8000"],
      "env": { "GPUMESH_TOKEN": "your-token" }
    }
  }
}
```

Prefer `env` over `--token` for the token: a config file is less likely to end
up in a process list or a shell history than a command line is.

## The tools

| Tool | What it does |
|---|---|
| `workers` | The machines in the mesh, with device, device name and benchmark score |
| `mesh_status` | Version, uptime, live/dead worker counts, total score, job counts |
| `submit_job` | Run a script across the mesh, once per payload. Returns a job id at once |
| `job_status` | Progress of one job: `finished`, and pending/running/done/failed counts |
| `job_result` | One entry per task, in submission order, with results or errors |
| `cancel_job` | Fail every pending and running task of a job |
| `radar_scan` | Listen for nearby machines broadcasting on the LAN |

`submit_job` does not block. The assistant submits, polls `job_status`, then
reads `job_result`, which is what you want for a sweep that takes minutes.

### What a job looks like

`script` is a standalone Python program. Each task runs it in a fresh
subprocess on some worker: the payload arrives as JSON on **stdin**, and the
script prints its result as JSON on the **last line of stdout**.

```python
import json, sys
p = json.load(sys.stdin)
print(json.dumps({"lr": p["lr"], "score": p["lr"] * 2}))
```

```json
[{"lr": 0.01}, {"lr": 0.05}, {"lr": 0.1}]
```

The scheduler reads four payload keys itself instead of passing them to the
script: `cost` weights the task and defaults to `1.0`, `gpu` names a device
such as `"cuda"` or `"A100"`, and `cpu_cores` and `gpu_memory_mb` set the
least capacity a worker must report.

Nothing from your machine reaches the script except what the payload carries.
The worker is a different machine with a different filesystem.

## What this does not change about trust

The assistant holds your token and gets no more authority than you have. It
cannot reach a mesh you could not reach from your own shell, and no tool
writes `~/.gpumesh/config.json`, because resolving a connection is a read.

Everything in [SECURITY.md](../SECURITY.md) still applies unchanged. A worker
runs submitted code as its own OS user and there is no sandbox. What changes
is who writes that code, so read what the assistant submits the way you would
read any other patch. `gpumesh serve --safe-mode` holds a coordinator to
script jobs, and `--strict` refuses pickled results on the submitting side.

## If it does not work

- **"No gpumesh coordinator is configured".** Run `gpumesh show-connection`.
  If it comes back empty, run `gpumesh serve --token <TOKEN>` or `gpumesh join
  <URL> --token <TOKEN>` first.
- **"Authentication failed".** The coordinator was restarted with a different
  token. Rejoin.
- **"unreachable".** The coordinator is down, or that address does not route
  from this machine. `gpumesh doctor` tells you which.
- **The client shows no gpumesh tools.** Check that `gpumesh mcp-serve` runs.
  It sits silently waiting on stdin, and Ctrl+C exits. Then check the
  assistant can find `gpumesh` on its `PATH`, and put an absolute path in
  `command` if it cannot.
