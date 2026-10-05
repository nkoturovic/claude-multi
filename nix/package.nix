# The claude-multi package: the stdlib-only launcher tree on nixpkgs python3,
# with no third-party Python dependencies. Default pkgs resolves the locked
# nixpkgs from the repository flake.lock, so plain
# `nix-build --no-out-link nix/package.nix` works without flake evaluation.
# cliProxyApiBin pins the CLIProxyAPI path into the claude-multi-proxy wrapper
# (and links it as libexec/claude-multi/cli-proxy-api, the gateway this
# package ships) when the flake supplies it; standalone builds resolve the
# gateway at runtime.
# It installs the Python package pyproject.toml declares (the runtime
# resources — catalog, schemas, version, the gateway contract and the pinned
# gateway's model registry — are package data under src/claude_multi/data),
# its documents and the public launchers among its console scripts, at the
# paths a wheel installs them.
# cliProxyApiRegistry is the pinned gateway source's embedded model registry
# directory (`${cliProxyApi.src}/internal/registry/models`): the build fails
# unless the checked-in registry snapshot equals it byte for byte.
{
  pkgs ?
    import
      (
        let
          lock = builtins.fromJSON (builtins.readFile ../flake.lock);
          node = lock.nodes.nixpkgs.locked;
        in
        fetchTarball {
          url = "https://github.com/${node.owner}/${node.repo}/archive/${node.rev}.tar.gz";
          sha256 = node.narHash;
        }
      )
      { },
  cliProxyApiBin ? null,
  cliProxyApiPatches ? [ ],
  cliProxyApiRegistry ? null,
  # The gateway contract manifest ({ version, upstream_version,
  # patches = [ "basename@sha256" ... ] } in applied order); evaluation fails
  # unless the checked-in src/claude_multi/data/gateway-contract.json is its
  # exact JSON.
  cliProxyApiContract ? null,
}:

let
  python = pkgs.python3;
  # The Python package declares what is installed (pyproject.toml): the
  # package under its package directory (with its runtime resources in
  # claude_multi/data), the documents and the console scripts. This package
  # installs that, where a wheel installs it: the package in site-packages,
  # the documents in share/claude-multi and one wrapper per public launcher
  # in bin/. Edits to tests/, AGENTS.md, gateway/ or nix/ never change the
  # store path (hence neither ExecStart nor the gateway).
  pyproject = builtins.fromTOML (builtins.readFile ../pyproject.toml);
  packageRoot = pyproject.tool.setuptools.package-dir."";
  dataFiles = pyproject.tool.setuptools.data-files;
  documents = builtins.concatLists (builtins.attrValues dataFiles);
  scripts = builtins.attrNames pyproject.project.scripts;
  # The installed commands are the product's public launchers, the set a
  # release bundle ships (packaging/product.json): the developer tool
  # (claude-multi-dev) stays a source checkout's entry point.
  launchers = (builtins.fromJSON (builtins.readFile ../packaging/product.json)).launchers;
  proxyScript = "claude-multi-proxy";
  launcherScripts = builtins.filter (name: name != proxyScript) launchers;
  installedPaths = [ packageRoot ] ++ documents;
  sitePackages = python.sitePackages;
  packageDir = "${sitePackages}/claude_multi";
  resources = "${packageDir}/data";
  # Never inherit management attestation from the caller. A standalone
  # wrapper may still resolve the binary at runtime, but cannot attest it.
  # Every wrapper names its install channel; management is enabled only on
  # this channel, and only with the attested gateway (management.py).
  channelFlag = ''--set CLAUDE_MULTI_CHANNEL nix'';
  proxyFlag =
    if cliProxyApiBin != null then
      ''--set CLAUDE_MULTI_PROXY_BIN "${cliProxyApiBin}" --set CLAUDE_MULTI_PROXY_PATCHES "${pkgs.lib.concatStringsSep "," cliProxyApiPatches}"''
    else
      ''--unset CLAUDE_MULTI_PROXY_BIN --unset CLAUDE_MULTI_PROXY_PATCHES'';
  # One content authority: the recipe's contract; the package ships the
  # checked-in resource, which must be its exact JSON.
  contractChecked =
    cliProxyApiContract == null
    || pkgs.lib.assertMsg
      (builtins.toJSON cliProxyApiContract == builtins.readFile ../src/claude_multi/data/gateway-contract.json)
      "src/claude_multi/data/gateway-contract.json is stale: run python3 tools/build.py gateway contract";
  # The gateway this package ships, at a path relative to the package root:
  # the supervised service tells a launcher-only change (reload) from a new
  # gateway binary (restart) by comparing it with the running gateway's.
  gatewayLink = pkgs.lib.optionalString (cliProxyApiBin != null) ''
    mkdir -p $out/libexec/claude-multi
    ln -s "${cliProxyApiBin}" $out/libexec/claude-multi/cli-proxy-api
  '';
  registryCheck = pkgs.lib.optionalString (cliProxyApiRegistry != null) ''
    for name in models.json codex_client_models.json; do
      cmp -s "${cliProxyApiRegistry}/$name" "$out/${resources}/registry/$name" || {
        echo "src/claude_multi/data/registry/$name differs from the pinned gateway source:" \
          "run python3 tools/build.py gateway fetch, then python3 tools/build.py gateway registry" >&2
        exit 1
      }
    done
  '';
  installDocuments = pkgs.lib.concatStrings (
    pkgs.lib.mapAttrsToList (directory: files: ''
      mkdir -p $out/${directory}
      cp ${pkgs.lib.concatStringsSep " " files} $out/${directory}/
    '') dataFiles
  );
in
assert contractChecked;
assert builtins.elem proxyScript scripts;
assert builtins.elem proxyScript launchers;
assert builtins.all (name: builtins.elem name scripts) launchers;
pkgs.stdenv.mkDerivation {
  pname = "claude-multi";
  version = (builtins.fromJSON (builtins.readFile ../src/claude_multi/data/version.json)).launcher_version;
  # Only the installed paths (installedPaths) enter the source.
  # Test-run __pycache__/.pyc artifacts must never reach the store: they would
  # both pollute the package and churn the source hash on every test run.
  src = pkgs.lib.cleanSourceWith {
    src = ../.;
    filter =
      path: _type:
      let
        rel = pkgs.lib.removePrefix (toString ../. + "/") (toString path);
        top = builtins.head (pkgs.lib.splitString "/" rel);
        base = baseNameOf (toString path);
        # A nested installed path (a document under docs/) admits its
        # parent directory, never that directory's other entries.
        admitted = builtins.elem top installedPaths || builtins.elem rel installedPaths
          || builtins.any (installed: pkgs.lib.hasPrefix (rel + "/") installed) installedPaths;
      in
      admitted && base != "__pycache__" && !(pkgs.lib.hasSuffix ".pyc" base);
  };
  nativeBuildInputs = [ pkgs.makeWrapper ];
  dontBuild = true;
  installPhase = ''
    runHook preInstall

    mkdir -p $out/${sitePackages} $out/bin
    cp -r ${packageRoot}/claude_multi $out/${packageDir}
    ${installDocuments}
    ${registryCheck}
    ${gatewayLink}
    # Unchecked-hash bytecode survives store mtime normalisation.
    ${python}/bin/python3 -m compileall -q -f -j "$NIX_BUILD_CORES" \
      --invalidation-mode unchecked-hash $out/${packageDir}

    # One wrapper per public launcher: the wrapper states the launch
    # environment and runs the same entry point a console script runs
    # (claude_multi.entrypoints); -P keeps the caller's directory off sys.path.
    for entry in ${pkgs.lib.concatStringsSep " " launcherScripts}; do
      makeWrapper ${python}/bin/python3 $out/bin/$entry \
        --set PYTHONPATH "$out/${sitePackages}" \
        --set CLAUDE_MULTI_ASSETS "$out/${resources}" \
        --set CLAUDE_MULTI_HOOK_COMMAND "$out/bin/claude-multi" \
        ${channelFlag} \
        --add-flags "-P -m claude_multi.entrypoints $entry"
    done

    makeWrapper ${python}/bin/python3 $out/bin/${proxyScript} \
      --set PYTHONPATH "$out/${sitePackages}" \
      --set CLAUDE_MULTI_ASSETS "$out/${resources}" \
      ${channelFlag} \
      ${proxyFlag} \
      --add-flags "-P -m claude_multi.entrypoints ${proxyScript}"

    runHook postInstall
  '';
  meta = {
    description = "claude-multi: durable multi-model launcher for Claude Code, with its gateway and onboarding tools";
    mainProgram = "claude-multi";
  };
}
