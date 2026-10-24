# Security model

RepoPilot runs code that a language model wrote, against repositories it did not write. Both are
treated as untrusted. This page describes each layer of protection and what each one does not
cover.

## 1. The container is the boundary

Agent edits and test runs happen in a throwaway container per run, created and driven only by
`sandbox-svc` (Go). The agent service never touches Docker: it calls sandbox-svc's REST API
with a bearer token.

| Control | Setting |
|---|---|
| Network | `--network none`: no egress, no ingress |
| User | uid/gid 10001, never root; `no-new-privileges`; all capabilities dropped |
| Filesystem | Read-only root; `/workspace` is a 512 MB tmpfs mounted `nodev,nosuid`; `/tmp` is a 256 MB tmpfs |
| Resources | 1 CPU, 1 GiB memory with no swap, 256 PIDs |
| Mounts | The workspace volume only: no host paths, no Docker socket, no secrets |
| Time | Every command runs under `timeout` (default 120 s, max 600 s), with a stream-deadline backstop |
| Leftovers | Processes a command leaves behind are killed. If not even that can start (a fork bomb that escaped `timeout`), the sandbox restarts and the workspace is restored |
| Output | stdout and stderr capped at 64 KiB each |
| Images | Only `repopilot-sandbox-*` and `repopilot-bench-*` images |
| Lifetime | Sandboxes older than 2 h are reaped, including orphans from a crashed service |

The container settings are in
[`internal/sandbox/manager.go`](../services/sandbox-svc/internal/sandbox/manager.go).

## 2. One small process holds the Docker socket

Access to the Docker socket is root-equivalent on the host, so exactly one process has it:
sandbox-svc. It is kept as small and locked down as possible:

- it is written in Go with the standard library only, so it has no third-party dependencies;
- it listens on loopback only and requires a token of at least 16 characters;
- it runs with a read-only filesystem and every capability dropped;
- it refuses images outside the allowlisted prefixes.

## 3. Files never cross the boundary through a shell

- **Uploads** are rewritten by sandbox-svc before Docker sees them:
  - only regular files, directories and symlinks are kept;
  - names must stay inside `/workspace`, and nothing may be written beneath a symlink;
  - setuid and setgid bits are cleared;
  - parent directories are emitted explicitly, so a symlink planted earlier is replaced, not
    followed.
- **Reads** run inside the sandbox as the sandbox user and open every path component with
  `O_NOFOLLOW`.

## 4. Tools the model cannot misuse

- **Tools unlock by phase.** Until the model submits a plan, it only has read-only tools. Each
  tool call is checked against exactly the set of tools offered for that reply.
- **Protected paths.** `ToolPolicy` denies writes to `.git/` (at any depth), `.github/`, and
  each task's protected globs. For the benchmark those are the held-out test names,
  `conftest.py` and the pytest configuration files.
- **The write policy trusts git, not a regex.** For `apply_patch`, the policy checks the paths
  that git itself reports (`git apply --numstat -z`). A hand-written header parser can be
  fooled by quoted names, non-`a/` prefixes, header-only, binary and renamed diffs.
- **Every denial is recorded** as an unauthorized modification attempt. Refused calls are also
  stored, so the audit trail shows what the model asked for as well as what ran.
- **Dispatch never raises.** Bad JSON, bad arguments, policy denials and sandbox errors all come
  back as a tool result the model can read and act on.

See [`tools/policy.py`](../services/agent-svc/src/repopilot/tools/policy.py) and
[`tools/builtin.py`](../services/agent-svc/src/repopilot/tools/builtin.py).

## 5. The harness validates, not the model

`finish` only asks for validation. The harness then:

1. diffs the workspace against `HEAD`, which covers deletions, both sides of renames and files
   hidden by `.gitignore`;
2. rejects the run if any protected path changed, independently of the tool policy;
3. runs the test command;
4. checks the workspace again after the tests, because test code can touch files too.

## 6. Secrets stay out of reach

- **The GitHub token** is used only on the host, to build the pull request through the Git
  Data API. It never enters a sandbox.
- **The API token** stays on the console's server. The browser talks to a server-side proxy
  that adds the token, so the token never reaches the page. The proxy refuses requests:
  - for any host other than the console's own, which stops DNS-rebinding pages;
  - from another origin;
  - with path segments that try to escape the API prefix.
- **The API** allows cross-origin requests only from the console's origin. `repopilot serve`
  refuses to bind beyond loopback while the API token is the shipped default or shorter than
  24 characters.

## What this is not

- **The command allowlist is defence in depth, not the boundary.** `python` can do anything a
  shell can. The container is the boundary.
- **sandbox-svc is root-equivalent on the host**, because it holds the Docker socket. That is
  why it is small and locked down, but a bug in it would matter.
- **Containers share the host kernel.** For untrusted public use, the next step is a sandboxed
  runtime such as gVisor or Firecracker. [deploy-aws.md](deploy-aws.md) shows where it plugs
  in.
- **The console has no login.** Anyone who can reach it can approve runs. Keep it on a private
  network or put an authenticating proxy in front of it.
