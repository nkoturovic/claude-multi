# Observation-only lifecycle diagnostics. Never linked into the shipped gateway.
# Invoke with the same cliProxyApi derivation as the ordinary gateway check.
{ cliProxyApi }:
let diagnostic = cliProxyApi.overrideAttrs (old: {
  pname = "gwtest-lifecycle-probes";
  goModules = cliProxyApi.goModules;
  doCheck = false;
  doInstallCheck = false;
  postCheck = "";
  postPatch = (old.postPatch or "") + ''
    cp ${./watcher_lifecycle_test.go} internal/watcher/gwtest_lifecycle_test.go
    cp ${./service_startup_window_test.go} sdk/cliproxy/gwtest_startup_window_test.go
  '';
  buildPhase = ''
    runHook preBuild
    export GOPROXY=off
    go test -mod=vendor -c -o gwtest-lifecycle-watcher ./internal/watcher
    go test -mod=vendor -c -o gwtest-lifecycle-service ./sdk/cliproxy
    runHook postBuild
  '';
  installPhase = ''
    mkdir -p "$out/bin" "$out/share"
    cp gwtest-lifecycle-watcher gwtest-lifecycle-service "$out/bin/"
    printf '%s\n' ${cliProxyApi} > "$out/share/gateway-outpath"
    printf '%s\n' ${cliProxyApi.src} > "$out/share/gateway-source"
    printf '%s\n' '${builtins.toJSON (map builtins.baseNameOf cliProxyApi.gatewayPatches)}' > "$out/share/gateway-patches.json"
  '';
});
# The vendor derivation the build really references, not the attribute.
in assert import ../vendor-inputs.nix diagnostic == [ cliProxyApi.goModules.drvPath ];
diagnostic
