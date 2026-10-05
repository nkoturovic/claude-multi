# The patched CLIProxyAPI gateway. gateway/UPSTREAM.json is the recipe and
# tools/build.py the one implementation: Nix fetches the recipe's pinned inputs
# as fixed-output derivations (the upstream source at its commit, the vendored
# Go modules, the official Go toolchain archive for the build host) and runs
# the same build steps offline over them, so a Nix build and a plain
# `python3 tools/build.py gateway` produce identical bytes.
#
# The value is the build host's gateway (bin/cli-proxy-api) with:
#   targets.<name>   one derivation per recipe target: the four shipped
#                    targets and the windows-amd64 compile canary
#   gates.portable   the portable Go test gates (CGO_ENABLED=0)
#   gates.race       the Linux race gates (cgo; never shipped)
#   inspect          a re-inspection of every shipped target's store output
#   repro            linux-amd64 built twice in different directories, compared
#   go               the pinned official toolchain unpacked (bin/go, bin/gofmt),
#                    for the development shell
#   src, goModules, goArchive, gatewayPatches, gatewayContract, licenses,
#   sbom, upstream
# Each shipped target's output also carries share/cli-proxy-api/sbom.cdx.json
# and share/cli-proxy-api/licenses/ (the checked-in notices, verified against
# the modules it links, and THIRD_PARTY_NOTICES.txt).
# Diagnostic builds override `patches` (omissions) and `buildPhase`
# (`go test -c`): unpack, patch and configure leave the prepared source tree
# as the working directory with the pinned `go` on PATH.
{ pkgs }:

let
  inherit (pkgs) lib;
  recipe = ../gateway/UPSTREAM.json;
  buildTool = ../tools/build.py;
  upstream = lib.importJSON recipe;
  version = upstream.upstream.version;

  hostTargets = {
    x86_64-linux = "linux-amd64";
    aarch64-linux = "linux-arm64";
    aarch64-darwin = "darwin-arm64";
    x86_64-darwin = "darwin-amd64";
  };
  buildSystem = pkgs.stdenv.buildPlatform.system;
  host = hostTargets.${buildSystem} or (throw "the gateway recipe pins no Go toolchain for ${buildSystem}");
  toolchain = upstream.toolchain.archives.${host};
  isLinux = pkgs.stdenv.buildPlatform.isLinux;

  repository = builtins.match "https://github.com/([^/]+)/([^/]+)" upstream.upstream.repository;
  src = pkgs.fetchFromGitHub {
    owner = builtins.elemAt repository 0;
    repo = builtins.elemAt repository 1;
    rev = upstream.upstream.commit;
    hash = upstream.source.nix_hash;
  };

  goArchive = pkgs.fetchurl {
    url = upstream.toolchain.url_prefix + toolchain.file;
    inherit (toolchain) sha256;
  };

  # The same archive unpacked as-is (GOROOT is the output; the official
  # binaries are used unmodified, so no fixup).
  go = pkgs.stdenvNoCC.mkDerivation {
    pname = "go-official";
    version = upstream.toolchain.version;
    src = goArchive;
    dontConfigure = true;
    dontBuild = true;
    dontFixup = true;
    installPhase = ''
      runHook preInstall
      cp -R . "$out"
      runHook postInstall
    '';
    meta.mainProgram = "go";
  };

  # The vendored module tree: `go mod vendor` with the pinned toolchain, run
  # by the same tool, which also checks the normalised tree hash.
  goModules = pkgs.stdenvNoCC.mkDerivation {
    name = "cli-proxy-api-${version}-go-modules";
    nativeBuildInputs = [ pkgs.python3 ];
    dontUnpack = true;
    dontConfigure = true;
    dontFixup = true;
    impureEnvVars = lib.fetchers.proxyImpureEnvVars;
    SSL_CERT_FILE = "${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt";
    outputHashMode = "recursive";
    outputHash = upstream.vendor.nix_hash;
    buildPhase = ''
      mkdir -p inputs
      ln -s ${src} inputs/source
      ln -s ${goArchive} inputs/${toolchain.file}
      python3 ${buildTool} gateway fetch vendor --upstream ${recipe} --inputs "$PWD/inputs" \
        --cache "$PWD/cache" --work "$PWD/work" --dist "$PWD/dist"
    '';
    installPhase = ''
      cp -r work/src/vendor "$out"
    '';
  };

  admitted = builtins.filter (entry: entry.admitted) upstream.series;
  # A patch file whose bytes differ from its recipe entry fails evaluation.
  patchFile =
    entry:
    let
      path = ../gateway/patches + "/${entry.basename}";
    in
    assert lib.assertMsg (builtins.hashFile "sha256" path == entry.sha256)
      "gateway/patches/${entry.basename} does not match its sha256 in gateway/UPSTREAM.json";
    path;
  gatewayPatches = map patchFile admitted;
  # The packaged gateway contract: the upstream version and the ordered
  # basename@sha256 identity of every applied patch. Every channel ships the
  # checked-in package resource src/claude_multi/data/gateway-contract.json,
  # which must be its exact bytes.
  contract = {
    version = 1;
    upstream_version = version;
    patches = map (entry: "${entry.basename}@${entry.sha256}") admitted;
  };
  gatewayContract =
    assert lib.assertMsg (builtins.toJSON contract == builtins.readFile ../src/claude_multi/data/gateway-contract.json)
      "src/claude_multi/data/gateway-contract.json is stale: run python3 tools/build.py gateway contract";
    contract;
  # The checked-in licence notices and per-target SBOMs; record checks them
  # against what each target links and copies them into the output.
  licenses = ../gateway/licenses;
  sbom = ../gateway/sbom;
  patchDir = pkgs.linkFarm "cli-proxy-api-patches" (
    map (entry: {
      name = entry.basename;
      path = patchFile entry;
    }) admitted
  );

  binaryName = target: upstream.upstream.binary + lib.optionalString (target.goos == "windows") ".exe";

  # Phases every gateway derivation shares. unpack verifies the inputs and
  # prepares the work tree, patch applies `$patches` in order with no fuzz,
  # configure exports the toolchain environment.
  prepared = {
    inherit version src goModules goArchive;
    patches = gatewayPatches;
    nativeBuildInputs = [
      pkgs.python3
      pkgs.git
    ];
    doCheck = false;
    dontFixup = true;
    unpackPhase = ''
      runHook preUnpack
      buildRoot=$PWD
      mkdir -p cm-inputs cm-patches
      ln -s "$src" cm-inputs/source
      ln -s "$goModules" cm-inputs/vendor
      ln -s "$goArchive" cm-inputs/${toolchain.file}
      gatewayBuild() {
        python3 ${buildTool} gateway "$@" --upstream ${recipe} --inputs "$buildRoot/cm-inputs" --offline \
          --cache "$buildRoot/cm-cache" --work "$buildRoot/cm-work" --dist "$buildRoot/cm-dist"
      }
      gatewayBuild fetch vendor
      cd cm-work/src
      runHook postUnpack
    '';
    patchPhase = ''
      runHook prePatch
      patchArgs=()
      for patch in $patches; do
        ln -s "$patch" "$buildRoot/cm-patches/$(stripHash "$patch")"
        patchArgs+=(--patch "$buildRoot/cm-patches/$(stripHash "$patch")")
      done
      gatewayBuild apply "''${patchArgs[@]}"
      runHook postPatch
    '';
    configurePhase = ''
      runHook preConfigure
      gatewayEnvironment=$(gatewayBuild env)
      eval "$gatewayEnvironment"
      runHook postConfigure
    '';
  };

  mkTarget =
    target:
    pkgs.stdenv.mkDerivation (
      prepared
      // {
        pname = "cli-proxy-api-${target.name}";
        buildPhase = ''
          runHook preBuild
          gatewayBuild build --target ${target.name}
          runHook postBuild
        '';
        installPhase = ''
          runHook preInstall
          gatewayBuild inspect record --target ${target.name} --licenses ${licenses} --sbom ${sbom}
          install -Dm0555 "$buildRoot/cm-dist/${target.name}/${binaryName target}" "$out/bin/${binaryName target}"
          install -Dm0444 "$buildRoot/cm-dist/BUILD.json" "$out/share/cli-proxy-api/BUILD.json"
          install -Dm0444 "$buildRoot/cm-dist/gateway-contract.json" "$out/share/cli-proxy-api/gateway-contract.json"
          # record writes the notices and SBOM for the admitted series only
          # (gateway-inspect requires them on every shipped output).
          if [ -e "$buildRoot/cm-dist/${target.name}/${binaryName target}.cdx.json" ]; then
            install -Dm0444 "$buildRoot/cm-dist/${target.name}/${binaryName target}.cdx.json" \
              "$out/share/cli-proxy-api/sbom.cdx.json"
            cp -r "$buildRoot/cm-dist/licenses" "$out/share/cli-proxy-api/licenses"
          fi
          runHook postInstall
        '';
        passthru = shared;
        meta = {
          description = "CLIProxyAPI ${version} with claude-multi's patches (${target.name})";
          license = lib.licenses.mit;
          mainProgram = upstream.upstream.binary;
        };
      }
    );

  targets = builtins.listToAttrs (map (target: lib.nameValuePair target.name (mkTarget target)) upstream.targets);
  shipped = builtins.filter (target: target.shipped) upstream.targets;

  mkGates =
    selection:
    pkgs.stdenv.mkDerivation (
      prepared
      // {
        pname = "cli-proxy-api-gates-${selection}";
        buildPhase = ''
          runHook preBuild
          gatewayBuild gates --gates ${selection}
          runHook postBuild
        '';
        installPhase = ''
          install -Dm0444 "$buildRoot/cm-work/gates.json" "$out/gates.json"
        '';
      }
    );

  gates = {
    portable = mkGates "portable";
  }
  // lib.optionalAttrs isLinux { race = mkGates "race"; };

  # The store outputs again: BUILD.json names the admitted series and each
  # binary's sha256; no store reference, static Linux, signed darwin/arm64;
  # every shipped output carries its SBOM and the licence notices.
  inspect = pkgs.runCommand "cli-proxy-api-inspect-${version}" { nativeBuildInputs = [ pkgs.python3 ]; } ''
    ${lib.concatMapStrings (target: ''
      cmp ${targets.${target.name}}/share/cli-proxy-api/sbom.cdx.json ${sbom}/${target.name}.cdx.json
      cmp ${targets.${target.name}}/share/cli-proxy-api/licenses/modules.json ${licenses}/modules.json
      test -s ${targets.${target.name}}/share/cli-proxy-api/licenses/THIRD_PARTY_NOTICES.txt
      mkdir -p dist-${target.name}/${target.name}
      cp ${targets.${target.name}}/bin/${binaryName target} dist-${target.name}/${target.name}/
      cp ${targets.${target.name}}/share/cli-proxy-api/BUILD.json dist-${target.name}/
      python3 ${buildTool} gateway inspect --upstream ${recipe} --work "$PWD/no-work" \
        --dist "$PWD/dist-${target.name}" --target ${target.name}
    '') shipped}
    touch $out
  '';

  repro =
    pkgs.runCommand "cli-proxy-api-repro-${version}"
      {
        nativeBuildInputs = [
          pkgs.python3
          pkgs.git
        ];
      }
      ''
        mkdir -p inputs
        ln -s ${src} inputs/source
        ln -s ${goModules} inputs/vendor
        ln -s ${goArchive} inputs/${toolchain.file}
        python3 ${buildTool} gateway repro --upstream ${recipe} --patches ${patchDir} \
          --inputs "$PWD/inputs" --offline --cache "$PWD/cache" --scratch "$PWD/scratch" \
          --target linux-amd64 > report.json
        install -Dm0444 report.json "$out/report.json"
      '';

  shared = {
    inherit
      upstream
      src
      go
      goModules
      goArchive
      gatewayPatches
      gatewayContract
      licenses
      sbom
      targets
      gates
      inspect
      repro
      ;
  };
in
targets.${host}
