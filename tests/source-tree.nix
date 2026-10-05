# The repository tree the sandbox checks stage (tests/default.nix,
# tests/gateway-check.nix): every product (src/ carries the package and its
# resources; pyproject.toml and MANIFEST.in describe it), test, tool, Nix,
# gateway, packaging (installers, runtime pins, product description), CI and
# documentation path; never VCS metadata, worktrees, build links or bytecode.
# flake.nix and flake.lock stay out, so the repository-layout tests keep
# their sandbox boundary.
{ lib }:

let
  root = ./..;
  tops = [
    ".github"
    "AGENTS.md"
    "CHANGELOG.md"
    "CONTRIBUTING.md"
    "LICENSE"
    "MANIFEST.in"
    "README.md"
    "SECURITY.md"
    "bin"
    "docs"
    "gateway"
    "nix"
    "packaging"
    "pyproject.toml"
    "src"
    "tests"
    "tools"
  ];
in
lib.cleanSourceWith {
  name = "claude-multi-tree";
  src = root;
  filter =
    path: _type:
    let
      rel = lib.removePrefix (toString root + "/") (toString path);
      top = builtins.head (lib.splitString "/" rel);
      base = baseNameOf (toString path);
    in
    builtins.elem top tops
    && base != "__pycache__"
    && !(lib.hasSuffix ".pyc" base);
}
