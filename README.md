# RepoPilot

A tool-using AI agent that investigates GitHub issues, edits code in a locked-down sandbox,
runs the tests, recovers from failures within fixed limits, and opens a draft pull request
only after a human approves.

**Status:** M0 — scaffold. See [docs/plan.md](docs/plan.md) for the roadmap.

## Sandbox security model

Agent edits and test runs happen in a throwaway container per run, created and driven only by
`sandbox-svc` (Go). The agent service never touches Docker; it calls sandbox-svc's REST API with
a bearer token.

What every sandbox container gets:

| Control | Setting |
|---|---|
| Network | `--network none`: no egress, no ingress |
| User | uid/gid 10001, never root; `no-new-privileges`; all capabilities dropped |
| Filesystem | read-only root; `/workspace` is a 512 MB tmpfs volume; `/tmp` is a 256 MB tmpfs |
| Resources | 1 CPU, 1 GiB memory with no swap, 256 PIDs (fork bombs stall, the host does not) |
| Mounts | the workspace volume only: no host paths, no Docker socket, no secrets |
| Time | every command runs under `timeout` (default 120 s, max 600 s); a child that keeps the output stream open past the deadline is killed by a backstop |
| Output | stdout and stderr capped at 64 KiB each |
| Images | only images whose names start with an allowed prefix (`repopilot-`) |
| Lifetime | sandboxes older than 2 h are reaped, including orphans from a crashed service |

Files cross the boundary through Docker's archive API, never through a shell. Reads refuse
symlinks and anything that is not a regular file, and paths cannot leave `/workspace`.

What this is **not**:

- **The command allowlist is defence in depth, not the boundary.** `exec` only accepts a short
  list of programs (`git`, `rg`, `python`, `pytest`, `ruff`, `ls`, `cat`, …) and no shells, but
  `python` can do anything a shell can. The container is the boundary.
- **sandbox-svc is root-equivalent on the host**, because it holds the Docker socket. That is why
  it is small, has no third-party dependencies, listens on loopback only, requires a token, and
  runs read-only with every capability dropped.
- **Containers share the host kernel.** A kernel exploit escapes them. For untrusted public use,
  the next step is a stronger runtime: gVisor (`runsc`) or Firecracker microVMs, which drop in
  at the container-create call.
