{
  description = "claude-multi: a durable multi-model launcher for Claude Code";

  # nixpkgs supplies the launcher's Python and the build tools only: the
  # gateway is built by tools/build.py from the pinned inputs in
  # gateway/UPSTREAM.json (official Go toolchain archive, upstream source,
  # vendored modules), so its bytes do not depend on this nixpkgs revision.
  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";
  };

  outputs = { self, nixpkgs, ... }:
    let
      lib = nixpkgs.lib;
      # Linux on both architectures and Apple silicon. The pinned nixpkgs no
      # longer supports Intel macOS; the release bundles cover that target.
      systems = [ "x86_64-linux" "aarch64-linux" "aarch64-darwin" ];
      forAllSystems = f: lib.genAttrs systems (system: f system nixpkgs.legacyPackages.${system});
      # The build host's gateway; .targets holds every recipe target.
      gatewayFor = pkgs: import ./nix/gateway.nix { inherit pkgs; };
      packageFor = pkgs: cliProxyApi: pkgs.callPackage ./nix/package.nix {
        cliProxyApiBin = "${cliProxyApi}/bin/cli-proxy-api";
        cliProxyApiPatches = map baseNameOf cliProxyApi.gatewayPatches;
        cliProxyApiContract = cliProxyApi.gatewayContract;
        cliProxyApiRegistry = "${cliProxyApi.src}/internal/registry/models";
      };
      app = pkg: name: description: {
        type = "app";
        program = "${pkg}/bin/${name}";
        meta.description = description;
      };
      # What the x86_64-linux-only checks build with.
      pkgs = nixpkgs.legacyPackages.x86_64-linux;
      cliProxyApi = self.packages.x86_64-linux.cli-proxy-api;
    in
    {
      packages = forAllSystems (system: pkgs:
        let cliProxyApi = gatewayFor pkgs;
        in {
          cli-proxy-api = cliProxyApi;
          # The shipped targets, cross-built with this host's pinned toolchain.
          gateway-linux-amd64 = cliProxyApi.targets.linux-amd64;
          gateway-linux-arm64 = cliProxyApi.targets.linux-arm64;
          gateway-darwin-arm64 = cliProxyApi.targets.darwin-arm64;
          gateway-darwin-amd64 = cliProxyApi.targets.darwin-amd64;
          claude-multi = packageFor pkgs cliProxyApi;
          default = self.packages.${system}.claude-multi;
        });

      apps = forAllSystems (system: _pkgs:
        let pkg = self.packages.${system}.claude-multi;
        in {
          default = app pkg "claude-multi" "Launch Claude Code with a multi-model lineup";
          claude-multi = app pkg "claude-multi" "Launch Claude Code with a multi-model lineup";
          claude-multi-proxy = app pkg "claude-multi-proxy" "The local gateway's tool (init, status, run, sign-ins)";
        });

      devShells = forAllSystems (system: pkgs: {
        default = import ./nix/devshell.nix {
          inherit pkgs;
          go = self.packages.${system}.cli-proxy-api.go;
        };
      });

      # Every system checks that its package builds. x86_64-linux also runs
      # the offline sandbox suite (tests/default.nix itself, so a flake check
      # sees only git-tracked files), the gateway contract harness (patched
      # gateway plus a trusted bubblewrap) and the recipe's checks: the Go
      # test gates, the inspection of the shipped targets, a two-directory
      # reproducibility build and the Windows compile canary.
      checks = lib.recursiveUpdate
        (forAllSystems (system: _pkgs: { package = self.packages.${system}.claude-multi; }))
        {
          x86_64-linux.claude-multi = import ./tests/default.nix { inherit pkgs; };
          x86_64-linux.claude-multi-gateway = import ./tests/gateway-check.nix { inherit pkgs cliProxyApi; };
          x86_64-linux.gateway-gates = cliProxyApi.gates.portable;
          x86_64-linux.gateway-race = cliProxyApi.gates.race;
          x86_64-linux.gateway-inspect = cliProxyApi.inspect;
          x86_64-linux.gateway-repro = cliProxyApi.repro;
          x86_64-linux.gateway-windows-canary = cliProxyApi.targets.windows-amd64;
        };
    };
}
