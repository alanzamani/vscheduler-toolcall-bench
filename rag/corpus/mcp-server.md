# vscheduler MCP server

Exposes the allowlisted `v*` commands as MCP tools, so an MCP client can drive
vscheduler from natural language.

```
Claude Code / Desktop  --MCP/http-->  mcp  --ssh-->  vscheduler  --ssh-->  nodeNN
                                    (this)       (runs vinfo, vkill, …)
```

The MCP container holds **no vscheduler code**. It reaches the vscheduler
container over SSH and runs one allowlisted command per call, the same way
production does. That keeps the LLM-facing process out of the scheduler's
privilege domain and works unchanged under Compose and Kubernetes.

## Run it

The MCP server is an optional add-on to any of the four stacks, switched on
and off with `./deploy` (see the top-level README):

```bash
./deploy compose dev up --mcp        # or, on a running stack: ./deploy <method> <mode> mcp on
./deploy compose dev status          # last line: MCP ENABLED/DISABLED + SSH health
```

| Stack | Endpoint | Default |
|---|---|---|
| compose dev | `http://localhost:8930/mcp` | read-write |
| compose prod | `http://localhost:8931/mcp` | read-only |
| k8s dev | `http://localhost:8940/mcp` | read-write |
| k8s prod | `http://localhost:8941/mcp` | read-only |

The examples below use the compose dev endpoint; substitute the port for the
others. `./deploy … status` already runs the probe below *and* calls
`vscheduler_health`; the raw curl is here for when you want to see the HTTP
yourself:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8930/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{
       "protocolVersion":"2024-11-05","capabilities":{},
       "clientInfo":{"name":"probe","version":"1"}}}'
# 200 = the server is serving. Anything else is a stack problem, not a
# client problem, so fix it here before touching Claude's settings.
```

Then connect a client. **Claude Code and Claude Desktop are separate
applications with separate configuration files**, and connecting one does
nothing for the other — the single most common way to get stuck here.

### Claude Code

```bash
claude mcp add --transport http vscheduler http://localhost:8930/mcp
claude mcp list     # expect: vscheduler: http://localhost:8930/mcp (HTTP) - ✔ Connected
```

Inside a session, `/mcp` lists the connected servers and their tools. Then
just type: *"which compute nodes exist and what state are they in?"*

### Claude Desktop

Desktop has **two** places that mention MCP, and only one of them is right
for this server:

| Screen | For | This server? |
|---|---|---|
| Settings → **Connectors** → Add custom connector | servers already running, reached over HTTP | **yes** |
| Settings → Developer → Local MCP servers | stdio servers Desktop launches itself (`command` + `args`) | no |

So: **Settings → Connectors → Add custom connector**, name `vscheduler`, URL
`http://localhost:8930/mcp`, then restart Desktop. The tools appear in the
chat box's connector menu.

A server added this way will **never** appear on the Developer screen. That
screen listing something else, or nothing, is not a sign of failure.

If Desktop refuses a plain-`http://localhost` URL, bridge it to stdio instead.
Settings → Developer → Edit config, and merge an `mcpServers` block into
`~/Library/Application Support/Claude/claude_desktop_config.json` (the file
usually exists already and holds unrelated keys — add to it, do not replace
it):

```json
{
  "mcpServers": {
    "vscheduler": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://localhost:8930/mcp", "--allow-http"]
    }
  }
}
```

That needs node on PATH; `npx` fetches `mcp-remote` on first launch. Added
this way it *does* show up under Developer, because Desktop is now the one
starting it.

### Asking for something

The tools are named after the CLI, but nothing has to be typed that way:

| Ask | Runs |
|---|---|
| "what compute nodes are there?" | `vinfo -v` |
| "usage report for alice" | `vreport -u alice -v` |
| "quota for bob on node02" | `vquota -u bob -n node02 -v` |
| "put node02 into maintenance" | `vset -n node02 maint -v` |
| "log alice off node03" | `vkill -n node03 -u alice -v` |

The last two change state and are tagged destructive, so a client can prompt
first. While experimenting, `MCP_READ_ONLY=true` leaves only the read-only
tools registered, so the others cannot be called even by name:

```bash
MCP_READ_ONLY=true ./deploy compose dev mcp on     # Compose; on k8s edit k8s/mcp/<mode>
```

## Tools

| Tool | Runs | Required | Changes state |
|---|---|---|---|
| `vinfo` | `vinfo -v` | — | no |
| `vreport` | `vreport [-u] [-n] [-d s e] -v` | — | no |
| `vquota` | `vquota [-u] [-n] -v` | — | no |
| `vmanage` | `vmanage -n <node> [-u] -v` | node | yes |
| `valloc` | `valloc -n <node> -u <user> -v` | node, user | yes |
| `vsync` | `vsync -n <node> [-u] -v` | node | yes |
| `vset` | `vset -n <node> <status> -v` | node, status | yes |
| `vkill` | `vkill -n <node> -u <user> -v` | node, user | yes |

Plus `vscheduler_health` (SSH reachability) and `list_vscheduler_commands`.

Set `MCP_READ_ONLY=true` to register only the read-only tools — the mutating
ones are then not registered at all, so they cannot be called even by name.

## Why the specs look the way they do

Three properties of the real CLI drive every entry in `commands.yaml`:

1. **No argparse.** `vscheduler/general/initiate.py` hand-parses `sys.argv` and
   understands only single-dash flags: `-u -n -d -t -v`. There is no `--node`.
2. **`-v` gates most output.** 225 prints across `src/vscheduler/` are gated
   on `MyPrintCondition.fprint`, true only when `-v` is in argv. Measured on
   the running stack: `vquota` and `vmanage` print nothing at all without it,
   `vreport` prints about half as much, `vinfo` is unaffected. Every spec
   carries `fixed_args: ["-v"]` since it is never harmful.
3. **Order matters.** `initiate.py` reads a bare status word from
   `sys.argv[arg+1]`, so positionals must follow their flags. The executor
   emits `binary, flags, positionals, fixed_args` — emit positionals first and
   `vset` silently parses an empty status.

**Omitting `-n` is not a harmless default.** Most tools read "no node" as
"every node in every partition". `vkill` then logs off every user on every
node (`kill.py:53`), and its confirmation prompt is commented out
(`kill.py:40-44`). Destructive specs therefore make `node` required, enforced
both by the JSON schema and again in `build_argv`.

## Output checks: re-verify after the redevelopment

A vscheduler command can exit 0 and still not have done its job. `vreport`
generates its figures in a child process whose exit status the CLI never
checks (`tools/report.py:80`), so the parent returns 0 whatever the child did.

Two things guard against that, and they catch different failures:

- **Noisy failures.** The executor flags any run that exits 0 while stderr
  carries a traceback, a `multiprocessing` child-death header, a bare
  `*Error`/`*Exception` line or a fatal signal.
- **Quiet failures.** An optional `expect:` block per command declares the
  files a run must have **freshly** written. Freshness, not existence, is the
  test: these figures are overwritten in place, so last week's PNGs would
  satisfy an existence check for as long as the command stayed broken.

Only `vreport` declares expectations. The other commands write to the
Guacamole or scheduler databases, where verification means a query rather than
a `stat`. Absent `expect:` means no check.

The check costs one extra SSH round trip per call. To turn it off:

```bash
MCP_SKIP_EXPECTATIONS=true ./deploy compose dev mcp on   # Compose; on k8s edit k8s/mcp/<mode>
```

**The expectations are per invocation shape, not per command**, because
`modules/reports/report_pool.py` branches on its arguments and writes a
different figure set in each branch:

| Invocation | Writes | Rendered by |
|---|---|---|
| no arguments | `fig1.png` + `fig2.png` | plotly/kaleido |
| `-u` **and** `-n` | `fig1.png` | matplotlib `plt.savefig` |
| `-u` *or* `-n` alone | `fig2.png` | plotly/kaleido |

That third column matters: the `-u -n` branch goes through matplotlib, so it
kept producing a figure during the period when every kaleido-backed path was
failing. A flat per-command file list would have been wrong for two of the
three shapes.

### Why this needs re-checking

**This table is a transcription of upstream control flow, and the
redevelopment will invalidate it.** It is the one part of `commands.yaml` that
depends on vscheduler's internals rather than on its CLI surface, and nothing
enforces that it stays true. A case that no longer matches reality fails in
one of two silent ways:

- the expected file has moved or is no longer written → a false alarm on every
  correct run, which teaches the reader to ignore the warning;
- a `when:` clause no longer matches any real invocation → a dead expectation
  that never fires and never complains, which is the exact failure the check
  exists to catch.

Only the first is visible. A malformed block — an unknown key, a `when:`
naming an argument that does not exist, a path escaping `dir` — is a hard
startup error, but a block that is merely *out of date* parses fine.

After any change to `report_pool.py`, `tools/report.py` or the report output
directory, re-verify:

```bash
# 1. which branches write which files, in the current source. Starts at the
#    "# visuals:" marker so the query-building block above it does not bury
#    the answer, skips comments, and keeps real line numbers.
awk '/# visuals:/{f=1} f && /savefig|write_image|^[[:space:]]*(if|elif|else)/ \
     && !/^[[:space:]]*#/ {print NR": "$0}' \
    src/vscheduler/modules/reports/report_pool.py

# 2. confirm against the running stack, one shape at a time
docker compose -f compose/base.yml -f compose/dev.yml exec vscheduler bash -lc \
  'D=$HOME/visualisation_scheduler/vscheduler/modules/reports; rm -f "$D"/*.png'
#    …call vreport with no args, then -u only, then -u with -n…
docker compose -f compose/base.yml -f compose/dev.yml exec vscheduler bash -lc \
  'stat -c "%n %s %Y" -- $HOME/visualisation_scheduler/vscheduler/modules/reports/*.png'

# 3. update the cases, then restart (in compose dev commands.yaml is
#    bind-mounted, so no rebuild for a YAML-only change)
./deploy compose dev mcp on
```

If a shape's file set becomes genuinely unknowable, give that case an empty
`files: []` and say why in a comment. An honest "not checked" is better than a
guess that fires at random.

## Adding a command

Edit `vscheduler_mcp/commands.yaml` and `./deploy compose dev mcp on` — in
compose dev the file is bind-mounted, so no rebuild. Every other stack bakes
it into the image; `mcp on` rebuilds it. Check the real flags in
`src/vscheduler/general/initiate.py` first; the names in `src/setup.py`
`console_scripts` are the only valid binaries.

`commands.yaml` is the security boundary: the executor will not run a binary
that is not listed, will not pass an argument whose value fails its `pattern`,
and rejects unknown argument names rather than ignoring them.

## Layout

```
mcp/
├── pyproject.toml
└── vscheduler_mcp/
    ├── commands.yaml   # the allowlist — the security boundary
    ├── config.py       # env-var settings + spec loading/validation
    ├── executor.py     # argv building, validation, SSH execution
    └── server.py       # generates one typed MCP tool per spec
```

Kept at `mcp/` rather than `src/`, because `src/` is the vScheduler
application source that the Docker images copy from.
