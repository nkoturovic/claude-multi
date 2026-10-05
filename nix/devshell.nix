# The development shell: the Python the package runs on, the Go toolchain the
# gateway builds with (the recipe's pinned official archive, never another
# downloaded toolchain), git and the tools the test suite calls (gnupg; on
# Linux flock from util-linux and bubblewrap for the network-isolated runs).
# Go caches stay under ~/.cache/cm-go, never ~/go.
{ pkgs, go }:

pkgs.mkShell {
  packages =
    [
      pkgs.python3
      go
      pkgs.git
      pkgs.gnupg
    ]
    ++ pkgs.lib.optionals pkgs.stdenv.hostPlatform.isLinux [
      pkgs.util-linux
      pkgs.bubblewrap
    ];
  shellHook = ''
    export GOPATH="$HOME/.cache/cm-go"
    export GOMODCACHE="$HOME/.cache/cm-go/mod"
    export GOCACHE="$HOME/.cache/cm-go/build"
    export GOTOOLCHAIN=local
    export PYTHONPATH="$PWD/src:$PWD/tests''${PYTHONPATH:+:$PYTHONPATH}"
  '';
}
