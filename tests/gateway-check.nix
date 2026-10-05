# The separate, network-hermetic gateway contract check. The main check
# remains tests/default.nix. Never accept fallback to an unsandboxed build.
{ pkgs, cliProxyApi }:
let startupProbe = import ./gateway-startup-probe.nix { inherit cliProxyApi; };
in pkgs.runCommand "claude-multi-gateway-tests"
  {
    nativeBuildInputs = [ pkgs.python3 pkgs.bubblewrap pkgs.openssl ];
    env.PYTHONDONTWRITEBYTECODE = "1";
    __noChroot = false;
    tree = import ./source-tree.nix { inherit (pkgs) lib; };
    inherit cliProxyApi;
  }
  ''
    export HOME="$TMPDIR/home"
    mkdir -p "$HOME" "$out"
    mkdir -m 0700 "$out/evidence"
    staged="$TMPDIR/staged/claude-multi"
    mkdir -p "$(dirname "$staged")"
    cp -r "$tree" "$staged"
    chmod -R u+w "$staged"
    export PYTHONPATH="$staged/src:$staged/tests"
    python3 "$staged/tests/_gateway_harness.py" --sandbox-proof
    export CLAUDE_MULTI_TEST_CLI_PROXY_API="$cliProxyApi/bin/cli-proxy-api"
    export CLAUDE_MULTI_TEST_GATEWAY_STARTUP_PROBE="${startupProbe}/bin/gwtest-startup-probe"
    export CLAUDE_MULTI_TEST_REQUIRE_GATEWAY=1
    export CLAUDE_MULTI_TEST_GATEWAY_EVIDENCE="$out/evidence"
    export CLAUDE_MULTI_TEST_KEYED_LANE=host
    export CLAUDE_MULTI_TEST_KEYED_EVIDENCE="$out/evidence"
    cd "$staged"
    modules=$(python3 tests/_gateway_harness.py --check-modules)
    set +e
    python3 -m unittest -v $modules > "$out/unittest.log" 2>&1
    status=$?
    set -e
    cat "$out/unittest.log"
    test "$status" -eq 0
    if grep -E ' \.\.\. skipped|skipped=' "$out/unittest.log"; then
      echo "a gateway test skipped inside the gateway check" >&2
      exit 1
    fi
    # tearDownModule publishes synchronously; missing files/rows are failures,
    # not successful atexit exceptions.
    python3 tests/_gateway_harness.py --validate-evidence "$out/evidence"
    python3 tests/_gateway_harness.py --validate-keyed-evidence "$out/evidence" --keyed-core-only
  ''
