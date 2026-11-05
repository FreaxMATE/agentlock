# Home Manager module:
#
#   imports = [ inputs.agentlock.homeManagerModules.default ];
#   programs.agentlock.enable = true;
{ self, agent-sandbox }:
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.programs.agentlock;
  toml = pkgs.formats.toml { };
in
{
  options.programs.agentlock = {
    enable = lib.mkEnableOption "agentlock, a sandbox for Claude Code";

    package = lib.mkOption {
      type = lib.types.package;
      default = import "${self}/nix/package.nix" {
        inherit pkgs;
        agentSandboxSrc = agent-sandbox;
      };
      defaultText = lib.literalMD "agentlock built from your pkgs";
      description = "The agentlock package.";
    };

    replaceClaude = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        Make plain `claude` (in any terminal, and VS Code's terminal mode)
        run sandboxed through agentlock. The unsandboxed one stays available
        as `claude-unsandboxed`.
      '';
    };

    settings = lib.mkOption {
      type = lib.types.nullOr toml.type;
      default = null;
      example = lib.literalExpression ''
        {
          tools.packages = [ "cargo" "rustc" ];
          network.host_ports = [ 5432 ];
        }
      '';
      description = ''
        Rules for every project, written to ~/.config/agentlock/config.toml.
        Leave it null to manage that file yourself (`agentlock allow ... --everywhere`
        writes to it, which only works when it is not managed here).
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    home.packages = [
      cfg.package
    ]
    ++ lib.optionals cfg.replaceClaude [
      # hiPrio: wins over a claude-code package installed elsewhere.
      (lib.hiPrio (
        pkgs.writeShellScriptBin "claude" ''
          AGENTLOCK_PASSTHROUGH=1 exec ${lib.getExe cfg.package} "$@"
        ''
      ))
      (pkgs.writeShellScriptBin "claude-unsandboxed" ''
        exec ${lib.getExe pkgs.claude-code} "$@"
      '')
    ];
    xdg.configFile."agentlock/config.toml" = lib.mkIf (cfg.settings != null) {
      source = toml.generate "agentlock-config.toml" cfg.settings;
    };
  };
}
