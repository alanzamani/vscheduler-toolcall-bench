# vscheduler_mcp (bundled copy)

The vScheduler MCP server, copied here so the benchmark runs on its own.
It comes from the vScheduler deployment repository, which is private,
as of 9 November 2026: the version the published results were scored
against.

The benchmark uses it for three things, and none of them reach a real
scheduler:

- `server.py` is started once (`toolbench tools`) to read the tool
  definitions exactly as an MCP client sees them.
- `commands.yaml` is the allowlist of commands and arguments the server
  will run.
- `executor.build_argv` checks a model's call against that allowlist, for
  the report's "allowlist would reject" column. Nothing is executed.

Keep this copy in step with the server when `commands.yaml` changes, or
the scores stop describing the tools a deployment really exposes.
