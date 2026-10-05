# Observation-only -race diagnostics. A SEPARATE derivation; it is never the
# shipped gateway and never feeds it. Invoke with the same cliProxyApi
# derivation as the gateway check.
{ cliProxyApi }:
let diagnostic = cliProxyApi.overrideAttrs (old: {
  pname = "gwtest-race-probes";
  # Reuse the pinned vendor derivation; renaming must not fork the FOD.
  goModules = cliProxyApi.goModules;
  doCheck = false;
  doInstallCheck = false;
  postCheck = "";
  postPatch = (old.postPatch or "") + ''
    cp ${./gateway_go/executor_race_test.go} internal/runtime/executor/gwtest_race_test.go
    cp ${./gateway_go/auth_race_test.go} sdk/cliproxy/auth/gwtest_race_test.go
    cp ${./gateway_go/api_race_test.go} internal/api/gwtest_race_test.go
    cp ${./gateway_go/service_race_test.go} sdk/cliproxy/gwtest_race_test.go
  '';
  buildPhase = ''
    runHook preBuild
    export GOPROXY=off CGO_ENABLED=1
    # Coverage marks target-path execution; -race needs atomic counters.
    race() {
      go test -mod=vendor -race -covermode=atomic -coverpkg="$3" -c -o "gwtest-race-$1" "$2"
    }
    race executor ./internal/runtime/executor ./internal/runtime/executor,./internal/auth/claude
    race auth ./sdk/cliproxy/auth ./sdk/cliproxy/auth
    race api ./internal/api ./internal/api,./sdk/api/handlers
    race service ./sdk/cliproxy ./internal/api,./sdk/api/handlers,./sdk/cliproxy,./sdk/cliproxy/auth,./internal/runtime/executor,./internal/auth/claude
    runHook postBuild
  '';
  installPhase = ''
    mkdir -p "$out/bin" "$out/share"
    cp gwtest-race-executor gwtest-race-auth gwtest-race-api gwtest-race-service "$out/bin/"
    # Target extents in THIS patched source: label, file, first, last line.
    # A func ends at its first column-0 brace (gofmt); a stmt is one line.
    # An optional 5th argument anchors a stmt inside the named func.
    target() {
      local label=$1 file=$2 kind=$3 pattern=$4 within=''${5:-} from=0 first last
      if [ -n "$within" ]; then
        from=$(grep -n -m1 -E "$within" "$file" | cut -d: -f1)
        test -n "$from" || { echo "gwtest: race anchor for $label not found" >&2; exit 1; }
      fi
      first=$(grep -n -E "$pattern" "$file" | awk -F: -v s="$from" '$1 > s && !found { print $1; found = 1 }')
      test -n "$first" || { echo "gwtest: race target $label not found" >&2; exit 1; }
      if [ "$kind" = func ]; then
        last=$(awk -v s="$first" 'NR > s && /^}/ { print NR; exit }' "$file")
      else
        last=$first
      fi
      printf '%s\t%s\t%s\t%s\n' "$label" "$file" "$first" "$last" >> "$out/share/targets.tsv"
    }
    target reconcile sdk/cliproxy/auth/conductor_selection.go func '^func \(m \*Manager\) ReconcileRegistryModelStates\('
    target reconcile_write sdk/cliproxy/auth/conductor_selection.go stmt '^[[:space:]]+auth\.ModelStates = candidateAuth\.ModelStates$' '^func \(m \*Manager\) ReconcileRegistryModelStates\('
    target update_internal sdk/cliproxy/auth/conductor_lifecycle.go func '^func \(m \*Manager\) updateInternal\('
    target clone_after_unlock sdk/cliproxy/auth/conductor_lifecycle.go stmt 'm\.scheduler\.upsertAuth\((authClone\.Clone\(\)|schedulerSnapshot)\)' '^func \(m \*Manager\) updateInternal\('
    target update_clients internal/api/server_reload.go func '^func \(s \*Server\) UpdateClientsContext\('
    target config_write internal/api/server_reload.go stmt '^[[:space:]]+s\.cfg = (cfg|snapshot)$' '^func \(s \*Server\) UpdateClientsContext\('
    target set_plugin_host sdk/api/handlers/handlers.go func '^func \(h \*BaseAPIHandler\) SetPluginHost\('
    target interceptor_host sdk/api/handlers/handlers_interceptors.go func '^func \(h \*BaseAPIHandler\) interceptorHost\('
    target unified_models internal/api/server_routes.go func '^func \(s \*Server\) unifiedModelsHandler\('
    target heartbeat internal/api/server_middleware.go func '^func \(s \*Server\) homeHeartbeatMiddleware\('
    target setup_token internal/runtime/executor/claude_executor_auth.go func '^func isClaudeSetupToken\('
    target executor_prepare internal/runtime/executor/claude_executor_auth.go func '^func \(e \*ClaudeExecutor\) PrepareRequestAuth\('
    target profile_fetch internal/runtime/executor/claude_executor_auth.go func '^func \(e \*ClaudeExecutor\) fetchClaudeOAuthProfile\('
    target manager_prepare sdk/cliproxy/auth/conductor_execution.go func '^func \(m \*Manager\) PrepareRequestAuth\('
    printf '%s\n' ${cliProxyApi} > "$out/share/gateway-outpath"
    printf '%s\n' ${cliProxyApi.src} > "$out/share/gateway-source"
    printf '%s\n' ${cliProxyApi.goModules} > "$out/share/go-modules"
    printf '%s\n' '${builtins.toJSON (map builtins.baseNameOf cliProxyApi.gatewayPatches)}' > "$out/share/gateway-patches.json"
  '';
});
# The vendor derivation the build really references, not the attribute.
in assert import ./vendor-inputs.nix diagnostic == [ cliProxyApi.goModules.drvPath ];
diagnostic
