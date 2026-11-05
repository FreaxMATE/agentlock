# The agentlock command.
{ pkgs, agentSandboxSrc }:
let
  lib = pkgs.lib;
  version = "0.1.0";

  # agent-sandbox.nix plus one small patch: a runtime list of extra
  # bubblewrap arguments, which agentlock uses to hide .env files and lock
  # parts of ~/.claude.
  agentSandbox = pkgs.applyPatches {
    name = "agent-sandbox-agentlock";
    src = agentSandboxSrc;
    patches = [ ../patches/extra-bwrap-args.patch ];
  };

  # The sandbox for the default settings, built ahead of time so the first
  # start (and every project without extra packages or network rules) is fast.
  defaultBuildSettings = {
    agent = "claude";
    packages = lib.sort lib.lessThan (lib.unique (builtins.fromJSON (builtins.readFile ../src/default-packages.json)));
    network = {
      mode = "open";
      domains = [ ];
      hostPorts = [ ];
    };
  };
  defaultBuildSettingsFile = pkgs.writeText "agentlock-default-settings.json" (builtins.toJSON defaultBuildSettings);
  defaultSandbox = import ./sandbox.nix {
    inherit pkgs agentSandbox;
    settings = defaultBuildSettings;
  };

  python = pkgs.python3.withPackages (ps: [ ps.tomlkit ]);
in
pkgs.runCommand "agentlock-${version}"
  {
    meta = {
      description = "Run Claude Code in a sandbox (agent-sandbox.nix + bubblewrap)";
      mainProgram = "agentlock";
      platforms = lib.platforms.linux;
    };
    passthru = { inherit defaultSandbox agentSandbox; };
  }
  ''
    mkdir -p $out/bin $out/share/agentlock
    cp -r ${./.} $out/share/agentlock/nix
    cp ${../src/agentlock.toml} $out/share/agentlock/agentlock.toml
    cp ${../src/default-packages.json} $out/share/agentlock/default-packages.json

    {
      echo "#!${python}/bin/python3 -P"
      cat ${../src/agentlock.py}
    } > $out/bin/agentlock
    chmod +x $out/bin/agentlock
    substituteInPlace $out/bin/agentlock \
      --replace-fail '@sandboxNix@' "$out/share/agentlock/nix/sandbox.nix" \
      --replace-fail '@agentSandbox@' '${agentSandbox}' \
      --replace-fail '@nixpkgs@' '${pkgs.path}' \
      --replace-fail '@system@' '${pkgs.stdenv.hostPlatform.system}' \
      --replace-fail '@defaultSandbox@' '${defaultSandbox}' \
      --replace-fail '@defaultBuildSettings@' '${defaultBuildSettingsFile}' \
      --replace-fail '@projectTemplate@' "$out/share/agentlock/agentlock.toml" \
      --replace-fail '@defaultPackages@' "$out/share/agentlock/default-packages.json" \
      --replace-fail '@version@' '${version}'
  ''
