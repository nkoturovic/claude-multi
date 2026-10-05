# Test-only diagnostic, never part of the shipped gateway. Build against the
# exact supplied gateway series (including each single-omission variant).
{ cliProxyApi }:
let diagnostic = cliProxyApi.overrideAttrs (old: {
  pname = "gwtest-startup-probe";
  # Renaming the diagnostic must not fork the vendored-modules FOD.
  goModules = cliProxyApi.goModules;
  doCheck = false;
  doInstallCheck = false;
  postCheck = "";
  postPatch = (old.postPatch or "") + ''
    cp ${./gateway_startup_probe_test.go} sdk/cliproxy/gwtest_startup_probe_test.go
    cp ${./gateway_service_probe_test.go} sdk/cliproxy/gwtest_omission_probe_test.go
    cp ${./gateway_codex_identity_probe_test.go} sdk/cliproxy/gwtest_codex_probe_test.go
    cp ${./gateway_executor_probe_test.go} internal/runtime/executor/gwtest_omission_probe_test.go
    cp ${./gateway_handlers_probe_test.go} sdk/api/handlers/gwtest_omission_probe_test.go
  '';
  buildPhase = ''
    runHook preBuild
    export GOPROXY=off
    # Keep the startup discriminator non-race: it observes the first-auth
    # barrier, not other omitted patches' concurrency. The three service
    # omission probes are deterministic receipt/ownership/parse checks.
    go test -mod=vendor -c -o gwtest-startup-probe ./sdk/cliproxy
    # The race detector needs cgo; the gateway itself builds with CGO_ENABLED=0.
    CGO_ENABLED=1 go test -mod=vendor -race -c -o gwtest-executor-probe ./internal/runtime/executor
    CGO_ENABLED=1 go test -mod=vendor -race -c -o gwtest-handlers-probe ./sdk/api/handlers
    runHook postBuild
  '';
  installPhase = ''
    mkdir -p "$out/bin"
    cp gwtest-startup-probe gwtest-executor-probe gwtest-handlers-probe "$out/bin/"
    mkdir -p "$out/share"
    printf '%s\n' ${cliProxyApi} > "$out/share/gateway-outpath"
  '';
});
# The vendor derivation the build really references, not the attribute.
in assert import ./vendor-inputs.nix diagnostic == [ cliProxyApi.goModules.drvPath ];
diagnostic
