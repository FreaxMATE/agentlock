# Builds one sandbox launcher from a resolved settings file.
#
# The agentlock CLI merges the defaults, the global config and the project's
# .agentlock.toml, keeps only the parts that need a build (agent, packages,
# network), writes them to a JSON file and runs:
#
#   nix-build <this file> --arg pkgs ... --argstr settingsFile /path/settings.json
#
# Everything else (which files to hide, extra folders, secrets, git rules) is
# decided at launch time and costs no rebuild.
{
  pkgs,
  agentSandbox, # the (patched) agent-sandbox.nix source
  settingsFile ? null,
  settings ? builtins.fromJSON (builtins.readFile settingsFile),
}:
let
  lib = pkgs.lib;
  sbx = import agentSandbox { inherit pkgs; };
  inSandbox = import ./in-sandbox.nix { inherit pkgs; };

  # Agent profiles. Adding another agent (codex, gemini, ...) means adding an
  # entry here: the package, the binary, the state it keeps in $HOME and the
  # domains it needs when the network is restricted.
  agents = {
    claude = {
      pkg = pkgs.claude-code;
      bin = "claude";
      rwDirs = [ "$HOME/.claude" ];
      # Keeps .claude.json inside ~/.claude (see agent-sandbox README).
      env.CLAUDE_CONFIG_DIR = "$HOME/.claude";
      domains = [
        "anthropic.com"
        "claude.com"
        "claude.ai"
      ];
    };
  };

  agent = agents.${settings.agent} or (throw "agentlock: unknown agent '${settings.agent}'. Known: ${lib.concatStringsSep ", " (builtins.attrNames agents)}");

  # "python3Packages.numpy" -> pkgs.python3Packages.numpy
  resolvePackage =
    name:
    let
      path = lib.splitString "." name;
    in
    if lib.hasAttrByPath path pkgs then
      lib.getAttrFromPath path pkgs
    else
      throw "agentlock: there is no package called '${name}' in nixpkgs (search on https://search.nixos.org/packages)";

  # What Claude starts as inside the sandbox: a small entry script that loads
  # the per-launch environment, then runs the agent.
  entry = pkgs.writeShellScriptBin "agentlock-entry" ''
    if [ -r /etc/agentlock/env ]; then
      set -a
      . /etc/agentlock/env
      set +a
    fi
    export GIT_CONFIG_GLOBAL=${inSandbox.gitconfig}
    if [ "''${AGENTLOCK_MODE:-agent}" = shell ]; then
      unset AGENTLOCK_MODE
      exec bash "$@"
    fi
    unset AGENTLOCK_MODE
    exec ${agent.pkg}/bin/${agent.bin} "$@"
  '';

  domains =
    if settings.network.mode == "open" then
      null
    else if settings.network.mode == "allowlist" then
      lib.unique (agent.domains ++ settings.network.domains)
    else if settings.network.mode == "off" then
      agent.domains
    else
      throw "agentlock: network.mode must be \"open\", \"allowlist\" or \"off\", not '${settings.network.mode}'";
in
sbx.mkSandbox {
  pkg = entry;
  binName = "agentlock-entry";
  outName = "agentlock-${settings.agent}";
  # The git wrapper comes first so it shadows the real git on PATH.
  allowedPackages = [ inSandbox.git ] ++ map resolvePackage settings.packages;
  rwDirs = agent.rwDirs;
  env = agent.env;
  workspaceDir = "$AGENTLOCK_WORKSPACE";
  allowedDomains = domains;
  allowedHostPorts = settings.network.hostPorts;
}
