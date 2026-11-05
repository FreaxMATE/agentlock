{
  description = "agentlock: run Claude Code in a sandbox, simply";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    agent-sandbox = {
      url = "github:archie-judd/agent-sandbox.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      agent-sandbox,
    }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
      pkgsFor =
        system:
        import nixpkgs {
          inherit system;
          config.allowUnfree = true; # claude-code
        };
    in
    {
      packages = forAllSystems (system: rec {
        agentlock = import ./nix/package.nix {
          pkgs = pkgsFor system;
          agentSandboxSrc = agent-sandbox;
        };
        default = agentlock;
      });

      # Builds agentlock from the pkgs of your system, so the sandbox uses
      # the same nixpkgs (and the same claude-code) as everything else.
      overlays.default = final: _prev: {
        agentlock = import ./nix/package.nix {
          pkgs = final;
          agentSandboxSrc = agent-sandbox;
        };
      };

      homeManagerModules.default = import ./nix/home-manager.nix { inherit self agent-sandbox; };
    };
}
