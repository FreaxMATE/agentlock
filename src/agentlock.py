"""agentlock: run Claude Code (and later other agents) in a sandbox.

  agentlock                      start Claude sandboxed in this folder
  agentlock status               show this project's rules and recent blocks
  agentlock allow ...            give Claude more access (see `agentlock help`)

Built on agent-sandbox.nix (bubblewrap). The rules come from three layers,
later ones win: built-in defaults, ~/.config/agentlock/config.toml (applies
everywhere) and .agentlock.toml in the project folder.
"""

from __future__ import annotations

import copy
import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tomllib
from pathlib import Path

import tomlkit

# Filled in by the Nix package.
SANDBOX_NIX = "@sandboxNix@"
AGENT_SANDBOX_SRC = "@agentSandbox@"
NIXPKGS = "@nixpkgs@"
SYSTEM = "@system@"
DEFAULT_SANDBOX = "@defaultSandbox@"
DEFAULT_BUILD_SETTINGS = "@defaultBuildSettings@"
PROJECT_TEMPLATE = "@projectTemplate@"
DEFAULT_PACKAGES = "@defaultPackages@"
VERSION = "@version@"

PROJECT_FILE = ".agentlock.toml"
HOME = Path.home()
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / "agentlock"
GLOBAL_FILE = CONFIG_DIR / "config.toml"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", HOME / ".cache")) / "agentlock"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", HOME / ".local" / "state"))
SESSIONS_DIR = STATE_DIR / "agent-sandbox"

EM_DASH = "\u2014"

DEFAULTS: dict = {
    "agent": "claude",
    "network": {
        "mode": "open",
        "domains": [],
        "host_ports": [],
    },
    "tools": {
        "packages": json.loads(Path(DEFAULT_PACKAGES).read_text()),
    },
    "files": {
        "read_only": [],
        "read_write": [],
        "hide": [".env", ".env.*"],
        "show": [".env.example", ".env.sample", ".env.template", ".env.dist"],
    },
    "secrets": {
        "inject": "all",
        "env_files": [".env"],
        "github_token": False,
    },
    "git": {
        "allow_commit": True,
        "allow_push": False,
        "reject_in_commit_message": [
            "co-authored-by:.*(claude|anthropic|openai|codex|copilot|gemini)",
            "generated (with|by) .*(claude|codex|copilot|gemini)",
            "noreply@anthropic",
            "\U0001F916",
            EM_DASH,
        ],
    },
}

# Lists that add up across layers (global + project) instead of replacing.
ADDITIVE = {
    ("network", "domains"),
    ("network", "host_ports"),
    ("tools", "packages"),
    ("files", "read_only"),
    ("files", "read_write"),
    ("files", "hide"),
}

# Folders never searched for files to hide (too big, or not the project's).
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".direnv",
             ".mypy_cache", ".pytest_cache", ".ruff_cache"}
MAX_DEPTH = 8

# Environment variables handed from VS Code / your shell to the agent.
PASS_PREFIXES = ("CLAUDE_", "ANTHROPIC_", "MCP_", "OTEL_", "DISABLE_", "MAX_THINKING")
PASS_NAMES = {"TERM", "COLORTERM", "TZ", "NO_COLOR", "FORCE_COLOR", "EDITOR", "VISUAL"}
# Never taken from .env files or passed through: the sandbox sets these.
RESERVED = {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "PWD", "OLDPWD",
            "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "NO_PROXY", "no_proxy",
            "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS", "REQUESTS_CA_BUNDLE", "CLAUDE_CONFIG_DIR",
            "PKG_CONFIG_PATH", "CLAUDE_CODE_SSE_PORT", "ALL_PROXY", "all_proxy",
            "NODE_TLS_REJECT_UNAUTHORIZED", "BASH_ENV", "ENV", "LANG"}
RESERVED_PREFIXES = ("GIT_", "LD_", "AGENTLOCK_", "BASH_FUNC_", "LC_")

# Project settings that widen Claude's access. If .agentlock.toml was changed
# by something other than agentlock (a git pull, a cloned repo, a hand edit),
# these are ignored until you run `agentlock trust`.
WIDENING = [
    ("network", "domains"), ("network", "host_ports"),
    ("files", "read_only"), ("files", "read_write"), ("files", "show"),
    ("secrets", "env_files"), ("secrets", "github_token"),
    ("git", "allow_push"), ("git", "reject_in_commit_message"),
]
TRUST_FILE = STATE_DIR / "agentlock" / "trusted.json"

# Parts of ~/.claude Claude may read but not change: changing them would
# let it run code outside the sandbox (hooks) or plant instructions for
# your other sessions.
CLAUDE_LOCKED_FILES = ["settings.json", "settings.local.json", "CLAUDE.md", "keybindings.json"]
CLAUDE_LOCKED_DIRS = ["hooks", "plugins", "skills", "agents", "commands", "output-styles", "rules"]


def say(msg: str) -> None:
    print(f"agentlock: {msg}", file=sys.stderr)


def die(msg: str, code: int = 1) -> None:
    say(msg)
    sys.exit(code)


# ---------------------------------------------------------------- config


def find_project_dir(start: Path) -> Path:
    """The git work tree root (if below your home folder), else `start`.

    Deliberately no search for the nearest .agentlock.toml: Claude can create
    files in subfolders, so a planted subfolder config must not count.
    """
    top = host_git("-C", str(start), "rev-parse", "--show-toplevel")
    if top:
        root = Path(top).resolve()
        if root != HOME and not HOME.is_relative_to(root) and start.is_relative_to(root):
            return root
    return start


def load_toml(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        die(f"{path} has a mistake in it: {e}")
    return {}


def merge(base: dict, over: dict, prefix: tuple = ()) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        key = prefix + (k,)
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge(out[k], v, key)
        elif key in ADDITIVE and isinstance(v, list):
            out[k] = list(dict.fromkeys([*out.get(k, []), *v]))
        else:
            out[k] = copy.deepcopy(v)
    return out


def validate(s: dict, where: str) -> None:
    """Wrong types fail loudly instead of silently meaning something else."""
    def want(sec, key, types, desc):
        v = s.get(sec, {}).get(key) if sec else s.get(key)
        if not isinstance(v, types):
            die(f"in {where}: {sec + '.' if sec else ''}{key} must be {desc}, not {v!r}")
        return v

    def str_list(sec, key):
        v = want(sec, key, list, "a list")
        if not all(isinstance(x, str) for x in v):
            die(f"in {where}: every entry of {sec}.{key} must be text in quotes, got {v!r}")

    want(None, "agent", str, "text")
    if want("network", "mode", str, "text") not in ("open", "allowlist", "off"):
        die(f'in {where}: network.mode must be "open", "allowlist" or "off"')
    str_list("network", "domains")
    ports = want("network", "host_ports", list, "a list of numbers")
    if not all(isinstance(p, int) and not isinstance(p, bool) and 0 < p < 65536 for p in ports):
        die(f"in {where}: network.host_ports must be port numbers without quotes, got {ports!r}")
    str_list("tools", "packages")
    for k in ("read_only", "read_write", "hide", "show"):
        str_list("files", k)
    inj = s["secrets"]["inject"]
    if inj != "all" and not (isinstance(inj, list) and all(isinstance(x, str) for x in inj)):
        die(f'in {where}: secrets.inject must be "all" or a list of names, got {inj!r}')
    str_list("secrets", "env_files")
    for sec, key in (("secrets", "github_token"), ("git", "allow_commit"), ("git", "allow_push")):
        want(sec, key, bool, "true or false (without quotes)")
    str_list("git", "reject_in_commit_message")


def file_hash(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def trusted_hashes() -> dict:
    try:
        return json.loads(TRUST_FILE.read_text())
    except (OSError, ValueError):
        return {}


def trust(path: Path) -> None:
    """Remember this exact content of a project config as yours."""
    t = trusted_hashes()
    t[str(path.resolve())] = file_hash(path)
    TRUST_FILE.parent.mkdir(parents=True, exist_ok=True)
    TRUST_FILE.write_text(json.dumps(t, indent=1))


def is_trusted(path: Path) -> bool:
    return trusted_hashes().get(str(path.resolve())) == file_hash(path)


def widening_entries(cfg: dict) -> list[tuple[str, str]]:
    return [(sec, key) for sec, key in WIDENING if key in cfg.get(sec, {})]


def notify(title: str, body: str) -> None:
    say(f"{title}: {body}")
    exe = shutil.which("notify-send")
    if exe and not sys.stderr.isatty():
        subprocess.run([exe, "-a", "agentlock", title, body], stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def load_settings(project: Path, launching: bool = False) -> dict:
    s = merge(DEFAULTS, load_toml(GLOBAL_FILE))
    validate(s, str(GLOBAL_FILE))
    pf = project / PROJECT_FILE
    cfg = load_toml(pf)
    risky = widening_entries(cfg)
    if risky and not is_trusted(pf):
        names = ", ".join(f"{a}.{b}" for a, b in risky)
        if launching and sys.stdin.isatty() and sys.stderr.isatty():
            say(f"{pf} was changed outside agentlock and gives Claude more access ({names}):")
            for sec, key in risky:
                print(f"    {sec}.{key} = {cfg[sec][key]!r}", file=sys.stderr)
            try:
                with open("/dev/tty") as tty:
                    print("agentlock: allow this? [y/N] ", end="", file=sys.stderr, flush=True)
                    ok = tty.readline().strip().lower() in ("y", "yes")
            except OSError:
                ok = False
            if ok:
                trust(pf)
                risky = []
        if risky:
            if launching:
                notify("agentlock: settings not applied",
                       f"{pf} gives Claude more access ({names}) but was not changed by agentlock. "
                       f"Those settings are ignored. If they are yours, run `agentlock trust` in {project}.")
            for sec, key in risky:
                del cfg[sec][key]
    s = merge(s, cfg)
    validate(s, str(pf))
    return s


def expand(p: str, base: Path) -> Path:
    path = Path(os.path.expandvars(os.path.expanduser(p)))
    return path if path.is_absolute() else (base / path)


# ---------------------------------------------------------------- build


def build_settings(s: dict) -> dict:
    """The part of the settings that needs a Nix build."""
    return {
        "agent": s["agent"],
        "packages": sorted(set(s["tools"]["packages"])),
        "network": {
            "mode": s["network"]["mode"],
            "domains": sorted(set(s["network"]["domains"])),
            "hostPorts": sorted(set(int(p) for p in s["network"]["host_ports"])),
        },
    }


def prune_cache(d: Path, keep: int) -> None:
    """Old sandboxes keep their Nix closures alive; drop all but the newest."""
    links = sorted((p for p in d.iterdir() if p.is_symlink()), key=lambda p: p.lstat().st_mtime, reverse=True)
    for link in links[keep:]:
        link.unlink(missing_ok=True)
        (d / f"{link.name}.json").unlink(missing_ok=True)


def sandbox_for(s: dict) -> Path:
    """Path of the sandbox launcher for these settings, building it if needed."""
    wanted = build_settings(s)
    try:
        if json.loads(Path(DEFAULT_BUILD_SETTINGS).read_text()) == wanted:
            return Path(DEFAULT_SANDBOX) / "bin" / f"agentlock-{wanted['agent']}"
    except OSError:
        pass
    blob = json.dumps(wanted, sort_keys=True)
    pins = "|".join([VERSION, SANDBOX_NIX, AGENT_SANDBOX_SRC, NIXPKGS, SYSTEM])
    key = hashlib.sha256((pins + blob).encode()).hexdigest()[:20]
    out = CACHE_DIR / "sandboxes" / key
    launcher = out / "bin" / f"agentlock-{wanted['agent']}"
    if launcher.exists():
        os.utime(out, follow_symlinks=False)  # recently used: keep it
        return launcher
    out.parent.mkdir(parents=True, exist_ok=True)
    settings_file = out.parent / f"{key}.json"
    settings_file.write_text(blob)
    notify("agentlock", "sandbox settings changed, preparing the sandbox (once per change, may take a minute) ...")
    expr = (
        "{ settingsFile }: import " + SANDBOX_NIX + " { "
        "pkgs = import " + NIXPKGS + " { system = \"" + SYSTEM + "\"; config.allowUnfree = true; }; "
        "agentSandbox = " + AGENT_SANDBOX_SRC + "; inherit settingsFile; }"
    )
    nix_build = shutil.which("nix-build") or "/run/current-system/sw/bin/nix-build"
    cmd = [nix_build, "--quiet", "--no-build-output", "-o", str(out),
           "--argstr", "settingsFile", str(settings_file), "-E", expr]
    # stdout must stay clean: VS Code talks to Claude over it.
    r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    prune_cache(out.parent, keep=10)
    if r.returncode != 0:
        notify("agentlock: building the sandbox failed",
               f"A typo in a package name in {PROJECT_FILE} or {GLOBAL_FILE} is the usual cause. "
               "Run `agentlock` in a terminal in the project to see the error.")
        sys.exit(1)
    return launcher


# ---------------------------------------------------------------- launch


def parse_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE lines as in .env files: export prefix, quotes (also spanning
    lines), comments."""
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return values
    lines = iter(text.splitlines())
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        value = value.lstrip()
        if value[:1] in ("'", '"'):
            q, body = value[0], value[1:]
            # Find the closing quote, reading further lines if needed.
            while True:
                i = 0
                end = -1
                while i < len(body):
                    if q == '"' and body[i] == "\\":
                        i += 2
                        continue
                    if body[i] == q:
                        end = i
                        break
                    i += 1
                if end >= 0:
                    body = body[:end]
                    break
                nxt = next(lines, None)
                if nxt is None:
                    break
                body += "\n" + nxt
            if q == '"':
                body = re.sub(r'\\(.)', lambda m: {"n": "\n", "t": "\t", "r": "\r"}.get(m.group(1), m.group(1)), body)
            value = body
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
        values[key] = value
    return values


def is_reserved(key: str) -> bool:
    return key in RESERVED or key.startswith(RESERVED_PREFIXES)


def files_to_hide(project: Path, s: dict) -> list[Path]:
    hide = [p.lower() for p in s["files"]["hide"]]
    show = [p.lower() for p in s["files"]["show"]]
    found: list[Path] = []
    base_depth = len(project.parts)
    for root, dirs, files in os.walk(project):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not os.path.islink(os.path.join(root, d))]
        if len(Path(root).parts) - base_depth >= MAX_DEPTH:
            dirs[:] = []
        for name in files:
            low = name.lower()
            if any(fnmatch.fnmatch(low, p) for p in hide) and not any(fnmatch.fnmatch(low, p) for p in show):
                p = Path(root) / name
                if p.is_symlink():
                    # A link into the project: hide what it points to.
                    # (Targets outside the project are invisible anyway.)
                    target = p.resolve()
                    if target.is_file() and target.is_relative_to(project):
                        found.append(target)
                elif p.is_file():
                    found.append(p)
    return sorted(set(found))


def host_git(*args: str) -> str:
    try:
        r = subprocess.run(["git", *args], capture_output=True, text=True, stdin=subprocess.DEVNULL)
        return r.stdout.strip() if r.returncode == 0 else ""
    except OSError:
        return ""


def gh_token() -> str:
    try:
        r = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, stdin=subprocess.DEVNULL)
        return r.stdout.strip() if r.returncode == 0 else ""
    except OSError:
        return ""


def prepare_claude_dir() -> list[Path]:
    """Make sure the parts of ~/.claude we lock exist (so they can be
    locked) and return everything to lock."""
    cdir = HOME / ".claude"
    cdir.mkdir(mode=0o700, exist_ok=True)
    settings = cdir / "settings.json"
    if not settings.exists() and not settings.is_symlink():
        settings.write_text("{}\n")
    if not (cdir / "CLAUDE.md").exists():
        (cdir / "CLAUDE.md").touch()
    for d in CLAUDE_LOCKED_DIRS:
        if not (cdir / d).is_symlink():
            (cdir / d).mkdir(exist_ok=True)
    # Inside the sandbox CLAUDE_CONFIG_DIR=~/.claude, so Claude keeps its
    # app state in ~/.claude/.claude.json. Start it from yours once.
    inner = cdir / ".claude.json"
    outer = HOME / ".claude.json"
    if not inner.exists() and outer.is_file():
        shutil.copy2(outer, inner)
        inner.chmod(0o600)

    lock = [cdir / n for n in CLAUDE_LOCKED_FILES + CLAUDE_LOCKED_DIRS]
    # Scripts your settings run (status line, hooks) may live in ~/.claude
    # too: lock every file under ~/.claude that settings.json mentions.
    try:
        text = settings.read_text()
        for m in re.finditer(r"(?:~|\$HOME|\$\{HOME\}|" + re.escape(str(HOME)) + r")/\.claude/[^\s\"'`;|&)]+", text):
            raw = m.group(0)
            for pre in ("~", "${HOME}", "$HOME"):
                if raw.startswith(pre + "/"):
                    raw = str(HOME) + raw[len(pre):]
            lock.append(Path(raw))
    except OSError:
        pass
    out = []
    for p in lock:
        if p.is_symlink():
            say(f"note: {p} is a symlink; Claude could replace the link. Make it a real file to lock it.")
            continue
        if p.exists() and p.resolve().is_relative_to(cdir):
            out.append(p)
    return out


def runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR")
    if base and Path(base).is_dir():
        root = Path(base) / "agentlock"
    else:
        root = Path(f"/tmp/agentlock-{os.getuid()}")
    root.mkdir(mode=0o700, exist_ok=True)
    st = root.lstat()
    if root.is_symlink() or st.st_uid != os.getuid() or st.st_mode & 0o077:
        die(f"{root} is not private to you; remove it and try again.")
    # Leftovers of sessions that were killed hard (they may hold secrets).
    for d in root.iterdir():
        if d.name.isdigit():
            try:
                os.kill(int(d.name), 0)
            except ProcessLookupError:
                shutil.rmtree(d, ignore_errors=True)
            except PermissionError:
                pass
    rt = root / str(os.getpid())
    shutil.rmtree(rt, ignore_errors=True)
    rt.mkdir(mode=0o700)
    return rt


def run(argv: list[str], shell: bool = False) -> int:
    # VS Code's claudeProcessWrapper calls us as: agentlock <its claude binary> <args>.
    # We run Nix's claude instead (the bundled one needs nix-ld, which the
    # sandbox does not have), so drop that path.
    if argv and os.path.isabs(argv[0]) and os.path.basename(argv[0]).startswith("claude") \
            and os.path.isfile(argv[0]) and os.access(argv[0], os.X_OK):
        argv = argv[1:]

    cwd = Path(os.getcwd()).resolve()
    if cwd == HOME or HOME.is_relative_to(cwd):
        die("refusing to run in your home folder (or above it): open a project folder instead "
            "(or use claude-unsandboxed).")
    project = find_project_dir(cwd)
    if not (project / PROJECT_FILE).exists():
        init_project(project, quiet=True)
    s = load_settings(project, launching=True)
    launcher = sandbox_for(s)
    os.umask(0o077)
    rt = runtime_dir()
    try:
        return launch(launcher, argv, cwd, project, s, rt, shell)
    finally:
        shutil.rmtree(rt, ignore_errors=True)


def launch(launcher: Path, argv: list[str], workspace: Path, project: Path, s: dict, rt: Path, shell: bool) -> int:
    binds: list[str] = []
    empty = rt / "empty"
    empty.touch()

    def ro(path: Path, dest: Path | None = None) -> None:
        binds.extend(["--ro-bind", str(path), str(dest or path)])

    cdir = HOME / ".claude"

    # 1. Extra folders from the config. First, so the hiding and locking
    #    below always win over them.
    for kind, flag in (("read_only", "--ro-bind"), ("read_write", "--bind")):
        for entry in s["files"][kind]:
            p = expand(entry, project).resolve()
            if not p.exists():
                say(f"skipping files.{kind} entry '{entry}': {p} does not exist.")
                continue
            if p == HOME or HOME.is_relative_to(p):
                say(f"skipping files.{kind} entry '{entry}': refusing to share your whole home folder.")
                continue
            if project.is_relative_to(p) or p.is_relative_to(project):
                say(f"skipping files.{kind} entry '{entry}': it overlaps the project folder itself.")
                continue
            if kind == "read_write" and (cdir.is_relative_to(p) or p.is_relative_to(cdir)):
                say(f"skipping files.{kind} entry '{entry}': ~/.claude is managed by agentlock.")
                continue
            binds.extend([flag, str(p), str(p)])

    # 2. Hide .env files and friends.
    hidden = files_to_hide(project, s)
    for f in hidden:
        ro(empty, f)

    # 3. Things Claude must not edit because they decide its rules, or run
    #    on your computer outside the sandbox. Locked as a whole folder,
    #    because a folder that is a mount point cannot be renamed away.
    ro(project / PROJECT_FILE)
    vscode = project / ".vscode"
    if not vscode.is_symlink():
        vscode.mkdir(exist_ok=True)
        ro(vscode)
    for p in prepare_claude_dir():
        ro(p)

    # 4. Per-launch files under /etc/agentlock (read-only inside).
    env: dict[str, str] = {}
    for k, v in os.environ.items():
        if (k in PASS_NAMES or k.startswith(PASS_PREFIXES)) and not is_reserved(k):
            env[k] = v
    # Only the built-in C.UTF-8 locale exists inside (no locale data).
    env["LANG"] = "C.UTF-8"
    inject = s["secrets"]["inject"]
    for name in s["secrets"]["env_files"]:
        values = parse_env_file(expand(name, project))
        for k, v in values.items():
            if is_reserved(k):
                continue
            if inject == "all" or k in inject:
                env[k] = v
    if s["secrets"]["github_token"]:
        tok = gh_token()
        if tok:
            env["GH_TOKEN"] = tok
            env["GITHUB_TOKEN"] = tok
        else:
            say("secrets.github_token is on, but `gh auth token` gave nothing (run `gh auth login`).")
    if shell:
        env["AGENTLOCK_MODE"] = "shell"
    env["AGENTLOCK"] = "1"
    (rt / "env").write_text("".join(f"{k}={shlex.quote(v)}\n" for k, v in sorted(env.items())))

    g = s["git"]
    (rt / "policy").write_text(
        f"AGENTLOCK_ALLOW_COMMIT={1 if g['allow_commit'] else 0}\n"
        f"AGENTLOCK_ALLOW_PUSH={1 if g['allow_push'] else 0}\n"
    )
    (rt / "commit-reject").write_text("".join(p + "\n" for p in g["reject_in_commit_message"]))

    def gitq(v: str) -> str:
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'

    name, email = host_git("config", "--global", "user.name"), host_git("config", "--global", "user.email")
    gc = "[user]\n"
    if name:
        gc += f"\tname = {gitq(name)}\n"
    if email:
        gc += f"\temail = {gitq(email)}\n"
    if not g["allow_push"]:
        # Second lock behind the git wrapper: every push URL is rewritten to
        # one that cannot work, even for the real git binary.
        for prefix in ("https://", "http://", "ssh://", "git://", "git@", "file://", "/"):
            gc += f'[url "agentlock-push-blocked:{prefix}"]\n\tpushInsteadOf = {gitq(prefix)}\n'
    (rt / "gitconfig").write_text(gc)
    for f in ("env", "policy", "commit-reject", "gitconfig"):
        ro(rt / f, Path("/etc/agentlock") / f)

    args_file = rt / "bwrap-args"
    args_file.write_text("\0".join(binds) + "\0")

    child_env = dict(os.environ)
    child_env.pop("AGENTLOCK_PASSTHROUGH", None)
    child_env["AGENT_SANDBOX_EXTRA_BWRAP_ARGS_FILE"] = str(args_file)
    child_env["AGENTLOCK_WORKSPACE"] = str(workspace)
    if hidden and sys.stderr.isatty():
        say("hidden from Claude: " + ", ".join(str(h.relative_to(project)) for h in hidden))

    # Ctrl-C in a terminal already reaches Claude directly (same process
    # group); forwarding it too would count as a second Ctrl-C.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    pending: list[int] = []
    proc: subprocess.Popen | None = None

    def forward(sig, _frame):
        if proc is None:
            pending.append(sig)
            return
        try:
            proc.send_signal(sig)
        except ProcessLookupError:
            pass

    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, forward)
    proc = subprocess.Popen([str(launcher), *argv], cwd=workspace, env=child_env,
                            preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))
    for sig in pending:
        proc.send_signal(sig)
    code = proc.wait()
    return 128 - code if code < 0 else code


# ---------------------------------------------------------------- init / allow / status


def init_project(project: Path, quiet: bool = False) -> None:
    target = project / PROJECT_FILE
    if target.exists():
        if not quiet:
            say(f"{target} already exists.")
        return
    target.write_text(Path(PROJECT_TEMPLATE).read_text())
    trust(target)
    say(f"created {target}: this project's sandbox rules (edit it any time).")


def edit_toml(path: Path, section: str, key: str, value, mode: str) -> None:
    """mode: 'add' to a list, 'remove' from a list, or 'set'."""
    doc = tomlkit.parse(path.read_text()) if path.exists() else tomlkit.document()
    if section not in doc:
        doc[section] = tomlkit.table()
    table = doc[section]
    if mode == "set":
        table[key] = value
    else:
        current = table.get(key)
        if current is None:
            current = tomlkit.array()
            table[key] = current
        if mode == "add" and value not in current:
            current.append(value)
        if mode == "remove":
            while value in current:
                current.remove(value)
    # Our edit keeps the file trusted if it was, or if nothing in it widened
    # access before (then the only widening part is the one just made here).
    was_trusted = (not path.exists() or path == GLOBAL_FILE or is_trusted(path)
                   or not widening_entries(load_toml(path)))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomlkit.dumps(doc))
    if path != GLOBAL_FILE:
        if was_trusted:
            trust(path)
        else:
            say(f"note: {path} also has changes not made by agentlock; review it and run `agentlock trust`.")


ALLOW_KINDS = {
    "domain": ("network", "domains"),
    "port": ("network", "host_ports"),
    "pkg": ("tools", "packages"),
    "ro": ("files", "read_only"),
    "rw": ("files", "read_write"),
}


def cmd_allow(args: list[str], remove: bool = False) -> int:
    everywhere = "--everywhere" in args
    args = [a for a in args if a != "--everywhere"]
    if len(args) < 2 or args[0] not in ALLOW_KINDS:
        die("usage: agentlock " + ("remove" if remove else "allow") +
            " domain|port|pkg|ro|rw <value>... [--everywhere]")
    kind, values = args[0], args[1:]
    section, key = ALLOW_KINDS[kind]
    project = find_project_dir(Path.cwd().resolve())
    target = GLOBAL_FILE if everywhere else project / PROJECT_FILE
    if not everywhere and not target.exists():
        init_project(project)
    for v in values:
        val: object = v
        if kind == "port":
            if not v.isdigit() or not 0 < int(v) < 65536:
                die(f"'{v}' is not a port number.")
            val = int(v)
        if kind in ("ro", "rw") and not remove:
            p = expand(v, project)
            if not p.exists():
                die(f"{p} does not exist.")
            # Store absolute (with ~) so the entry means the same in every project.
            p = p.resolve()
            val = "~/" + str(p.relative_to(HOME)) if p.is_relative_to(HOME) else str(p)
        edit_toml(target, section, key, val, "remove" if remove else "add")
        say(("removed " if remove else "allowed ") + f"{kind} {val} in {target}")
    if kind == "domain" and not remove:
        s = load_settings(project)
        if s["network"]["mode"] == "open":
            say("note: the network is open anyway; domains only matter with `agentlock network allowlist`.")
    if kind == "ro" or kind == "rw":
        say("applies from the next Claude start.")
    else:
        say("applies from the next Claude start (the first start after a change takes a bit longer).")
    return 0


def cmd_network(args: list[str]) -> int:
    everywhere = "--everywhere" in args
    args = [a for a in args if a != "--everywhere"]
    if len(args) != 1 or args[0] not in ("open", "allowlist", "off"):
        die("usage: agentlock network open|allowlist|off [--everywhere]\n"
            "  open       whole internet (default)\n"
            "  allowlist  only Claude itself plus the domains you allowed\n"
            "  off        only Claude itself")
    project = find_project_dir(Path.cwd().resolve())
    target = GLOBAL_FILE if everywhere else project / PROJECT_FILE
    edit_toml(target, "network", "mode", args[0], "set")
    say(f"network set to '{args[0]}' in {target}; applies from the next Claude start.")
    return 0


BLOCK_RE = re.compile(r"^(\S+) blocked:? (.*)$")


def recent_blocks(limit_sessions: int = 10) -> list[tuple[str, str, str]]:
    out = []
    if not SESSIONS_DIR.is_dir():
        return out
    sessions = sorted([d for d in SESSIONS_DIR.iterdir() if d.is_dir() and "agentlock-" in d.name], reverse=True)
    for d in sessions[:limit_sessions]:
        for log in ("proxy.log", "launch.log"):
            try:
                lines = (d / log).read_text(errors="replace").splitlines()
            except OSError:
                continue
            for line in lines:
                m = BLOCK_RE.match(line)
                if m:
                    out.append((d.name, m.group(1), m.group(2)))
                elif log == "launch.log" and ("ERROR" in line or "refus" in line.lower()):
                    out.append((d.name, "", line.strip()))
    return out


def cmd_status() -> int:
    project = find_project_dir(Path.cwd().resolve())
    s = load_settings(project)
    pf = project / PROJECT_FILE
    print(f"project:   {project}")
    print(f"config:    {pf if pf.exists() else '(none yet, created at first start)'}")
    print(f"global:    {GLOBAL_FILE if GLOBAL_FILE.exists() else '(none)'}")
    n = s["network"]
    print(f"network:   {n['mode']}" + (f", domains: {', '.join(n['domains'])}" if n["domains"] else ""))
    print(f"localhost: {', '.join(map(str, n['host_ports'])) or 'blocked'}")
    print(f"extra rw:  {', '.join(s['files']['read_write']) or '-'}")
    print(f"extra ro:  {', '.join(s['files']['read_only']) or '-'}")
    hidden = files_to_hide(project, s)
    print(f"hidden:    {', '.join(str(h.relative_to(project)) for h in hidden) or '-'}")
    inj = s["secrets"]["inject"]
    print(f"secrets:   {'all keys from ' + ', '.join(s['secrets']['env_files']) if inj == 'all' else (', '.join(inj) or 'none')}"
          + (" + GitHub token" if s["secrets"]["github_token"] else ""))
    g = s["git"]
    print(f"git:       commit {'allowed' if g['allow_commit'] else 'blocked'}, push {'allowed' if g['allow_push'] else 'blocked'}")
    extra = [p for p in s["tools"]["packages"] if p not in DEFAULTS["tools"]["packages"]]
    print(f"tools:     defaults{' + ' + ', '.join(extra) if extra else ''}")
    blocks = recent_blocks()
    print()
    if not blocks:
        print("No blocked requests in the recent sessions.")
        return 0
    print("Recently blocked (newest session first):")
    seen = set()
    for session, ts, what in blocks:
        if what in seen:
            continue
        seen.add(what)
        print(f"  {ts[:19]}  {what}")
        m = re.match(r"\S+ ([^/\s:]+)", what)
        if m and "allowed" in what:
            print(f"      -> to allow: agentlock allow domain {m.group(1)}")
    return 0


HELP = """agentlock: runs Claude Code in a sandbox.

  agentlock [claude options]       start Claude in this folder, sandboxed
  agentlock shell                  open a shell inside the same sandbox (to look around)
  agentlock status                 show the rules for this project and what was blocked

  agentlock allow domain <d>...    let Claude reach a website (when network is 'allowlist')
  agentlock allow port <n>...      let Claude reach a service on this computer (localhost:<n>)
  agentlock allow pkg <name>...    add a program from nixpkgs (e.g. cargo, go, postgresql)
  agentlock allow ro <path>...     let Claude read a folder or file outside the project
  agentlock allow rw <path>...     let Claude read and change a folder outside the project
  agentlock remove <same as allow> take it back
  agentlock network open|allowlist|off

  Add --everywhere to allow/remove/network to change it for all projects
  (~/.config/agentlock/config.toml) instead of only this one (.agentlock.toml).

  agentlock trust                  accept .agentlock.toml after you edited it by hand
                                   (or after a git pull/clone changed it)
  agentlock init                   create .agentlock.toml here (also happens at first start)
  agentlock config                 print where the config files are

Changes apply the next time Claude starts (in VS Code: open a new Claude tab).
"""


def cmd_trust() -> int:
    project = find_project_dir(Path.cwd().resolve())
    pf = project / PROJECT_FILE
    if not pf.exists():
        die(f"there is no {pf}.")
    cfg = load_toml(pf)
    risky = widening_entries(cfg)
    if risky:
        print(f"{pf} gives Claude this extra access:")
        for sec, key in risky:
            print(f"    {sec}.{key} = {cfg[sec][key]!r}")
    trust(pf)
    say(f"trusted {pf} (until it changes again outside agentlock).")
    return 0


def main() -> int:
    argv = sys.argv[1:]
    # Installed as `claude`: everything goes to Claude, no agentlock commands.
    if os.environ.get("AGENTLOCK_PASSTHROUGH") == "1":
        return run(argv)
    cmd = argv[0] if argv else ""
    if cmd in ("help", "--help-agentlock", "-h") and len(argv) == 1:
        print(HELP)
        return 0
    if cmd == "status":
        return cmd_status()
    if cmd == "allow":
        return cmd_allow(argv[1:])
    if cmd == "remove":
        return cmd_allow(argv[1:], remove=True)
    if cmd == "network":
        return cmd_network(argv[1:])
    if cmd == "trust":
        return cmd_trust()
    if cmd == "init":
        init_project(find_project_dir(Path.cwd().resolve()))
        return 0
    if cmd == "config":
        print(f"project: {find_project_dir(Path.cwd().resolve()) / PROJECT_FILE}")
        print(f"global:  {GLOBAL_FILE}")
        return 0
    if cmd == "shell":
        return run(argv[1:], shell=True)
    return run(argv)


if __name__ == "__main__":
    sys.exit(main())
