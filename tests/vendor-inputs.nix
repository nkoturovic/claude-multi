# The vendored modules as the build consumes them: the go-modules
# derivations referenced by ANY attribute of a diagnostic's derivation
# (string context), never the goModules attribute the diagnostic itself
# set. Evaluation only; asserting on it adds nothing to (and never changes)
# the diagnostic derivation.
diagnostic:
let
  flatten = value:
    if builtins.isList value then builtins.concatStringsSep " " (map flatten value)
    else if builtins.isAttrs value && value ? outPath then toString value
    else if builtins.isAttrs value then flatten (builtins.attrValues value)
    else if builtins.isString value then value
    else "";
  context = builtins.getContext (flatten (builtins.attrValues diagnostic.drvAttrs));
in
builtins.filter (name: builtins.match ".*-go-modules[.]drv" name != null) (builtins.attrNames context)
