# The Retry-After candidate gateway, for observation ONLY, never the shipped
# package. Supply the dependency-closed candidate prefix in order: 8–11 or 8–10.
{ cliProxyApi, extraPatches }:
assert builtins.length extraPatches == 3 || builtins.length extraPatches == 4;
let diagnostic = cliProxyApi.overrideAttrs (old: {
  pname = "gwtest-retry-after-diagnostic";
  # No new fixed-output derivation or module download.
  goModules = cliProxyApi.goModules;
  patches = (old.patches or [ ]) ++ extraPatches;
  doCheck = false;
  doInstallCheck = false;
  postCheck = "";
  postInstall = (old.postInstall or "") + ''
    mkdir -p "$out/share/gwtest"
    printf '%s\n' ${cliProxyApi.goModules} > "$out/share/gwtest/go-modules"
    printf '%s\n' ${cliProxyApi} > "$out/share/gwtest/baseline"
    printf '%s\n' '${builtins.toJSON (map (p: { name = builtins.baseNameOf (toString p); sha256 = builtins.hashFile "sha256" p; }) extraPatches)}' > "$out/share/gwtest/extra-patches.json"
  '';
});
# The vendor derivation the build really references, not the attribute.
in assert import ./vendor-inputs.nix diagnostic == [ cliProxyApi.goModules.drvPath ];
diagnostic
