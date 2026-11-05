# agentlock

Run Claude Code in a sandbox on Linux, without thinking about it.

> [!WARNING]
> **This is a seatbelt, not a vault.** agentlock makes it easy to keep Claude
> away from things it does not need: your other projects, SSH keys, `.env`
> secrets, local services. It is meant to stop accidents and limit the damage
> of a confused or tricked agent. It is **not** a hardened security boundary
> and does not try to be one: parts of it can be worked around. If you need
> real isolation from untrusted code, use a separate VM or machine.
> Read [Limits](#limits) once.

## What Claude can and cannot do

| | Default | Change it with |
|---|---|---|
| This project folder | read and write | |
| Rest of your computer | invisible | `agentlock allow ro <path>` / `allow rw <path>` |
| `.env`, `.env.*` files | look empty | `[files] hide / show` |
| Values from `.env` | given as environment variables | `[secrets] inject` |
| Internet | open | `agentlock network allowlist` / `off` |
| Services on localhost | blocked | `agentlock allow port 5432` |
| Programs | git, gh, curl, jq, rg, fd, node, python3, uv, make, ... | `agentlock allow pkg cargo` |
| Your login, history, memory (`~/.claude`) | shared | |
| Your Claude settings, hooks, plugins, skills, CLAUDE.md | read only | |
| `.agentlock.toml`, `.vscode/` (created if missing) | read only | |
| `git commit` | allowed, unsigned | `[git] allow_commit` |
| Commit messages with Claude/AI attribution or em dashes | rejected | `[git] reject_in_commit_message` |
| `git push` | blocked | `[git] allow_push` |
| GitHub login (`gh auth token`) | not given | `[secrets] github_token = true` |

Anything Claude changes outside the project folder (for example in its own
home folder) is thrown away when it exits.

## Install

For NixOS with flakes and Home Manager. Other distros with Nix and Home
Manager should work too, but are untested.

1. Add the input to your flake:

   ```nix
   inputs.agentlock = {
     url = "github:FreaxMATE/agentlock";
     inputs.nixpkgs.follows = "nixpkgs";
   };
   ```

2. Enable it in your Home Manager config:

   ```nix
   imports = [ inputs.agentlock.homeManagerModules.default ];
   programs.agentlock.enable = true;
   # programs.agentlock.replaceClaude = false;  # keep plain `claude` unsandboxed
   # programs.agentlock.settings = { network.host_ports = [ 5432 ]; };  # all projects
   ```

3. Rebuild (`sudo nixos-rebuild switch --flake .`).

4. Only after the rebuild, point the VS Code extension at it in `settings.json`
   (`which agentlock` shows the path):

   ```json
   "claudeCode.claudeProcessWrapper": "/etc/profiles/per-user/<you>/bin/agentlock"
   ```

## Use

* **VS Code:** nothing to do. Every Claude panel runs in the sandbox.
* **Terminal:** `claude` as always. Without the sandbox: `claude-unsandboxed`.

The first start in a project creates a commented `.agentlock.toml` there.
Commit it or ignore it, as you like.

Something blocked? Run `agentlock status` in the project. It shows the rules,
what was blocked recently and the command to allow it.

```
agentlock allow domain pypi.org          # a website (when network is 'allowlist')
agentlock allow port 5432                # a service on this computer
agentlock allow pkg postgresql           # a program from nixpkgs
agentlock allow ro ~/src/other-project   # read a folder
agentlock allow rw ~/notes/project-x     # read and change a folder
agentlock remove ro ~/src/other-project  # take it back
agentlock network allowlist              # only Claude + allowed domains
agentlock network open                   # whole internet again
agentlock shell                          # look around inside the sandbox
```

Add `--everywhere` to change a rule for all projects
(`~/.config/agentlock/config.toml`). Changes apply the next time Claude starts
(VS Code: open a new Claude tab).

If `.agentlock.toml` gives Claude more access and was not changed by
`agentlock allow/remove/network` (you edited it, or a `git pull` or clone
brought it), those settings are ignored until you run `agentlock trust`.
Settings that only restrict Claude always apply.

## Limits

* **Claude can read everything it is given:** injected `.env` values, its
  login token and the transcripts of your other projects in
  `~/.claude/projects`. With the network open, a tricked Claude (for example
  by instructions hidden in a web page or an issue) could send those
  somewhere. For sensitive projects use `inject = [...]` and
  `agentlock network allowlist`.
* **The git rules stop accidents, not attacks.** Claude could call the real
  git binary by its full path.
* **Code Claude writes runs with your rights when you run it.** That includes
  `Makefile`, `package.json` scripts, `flake.nix` shell hooks, `.envrc`,
  Claude hooks (`.claude/settings.json`) and MCP servers (`.mcp.json`).
  Review diffs before running them, and be careful with `claude-unsandboxed`
  in a project Claude has worked on.
* **`.env` files are hidden as they are at start.** One created or replaced
  during a session stays visible until Claude restarts.
* **It is only as strong as bubblewrap and your kernel.** It does not hide
  your hostname, username or hardware, and does not protect against kernel
  bugs.

## How it works

* [agent-sandbox.nix](https://github.com/archie-judd/agent-sandbox.nix)
  builds a bubblewrap sandbox with only the listed programs, a private home
  and `/tmp`, and network filtering.
* A small patch adds extra mounts decided at every start: empty files over
  `.env`, read-only Claude settings, the extra folders you allowed.
* `.env` values are written to a file in `$XDG_RUNTIME_DIR` (memory only,
  deleted on exit) and loaded inside the sandbox.
* Inside, `git` is a wrapper that blocks push and `--no-verify`, checks
  commit messages and then runs the project's own hooks.
* Sandboxes for non-default programs or network rules are built on first use
  and cached in `~/.cache/agentlock`.

## Development

```
nix build
tests/smoke.sh        # checks the rules in a throwaway project, without starting Claude
```

Other agents (Codex, Gemini, OpenCode) are one entry each in the profile
table in `nix/sandbox.nix`. Only `claude` exists so far.

## License

MIT, see [LICENSE](LICENSE).
