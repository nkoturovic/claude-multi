#!/bin/sh
# Reviewed hosted-CI reproduction only. No additional namespace or egress claim.
set -eu
umask 077
home=/home/journey
scratch=$home/diag

case ${1:-} in
stage)
    # Installation/staging only; never run a client as root.
    harness=${2:?harness scripts}
    candidate=${3:?pristine candidate scripts}
    metadata=${4:?Windows-local metadata through drvfs}
    [ "$(id -u)" -eq 0 ]
    chown journey:journey "$metadata"
    chmod 0777 "$metadata"
    mkdir "$scratch"
    cp "$harness/wsl_diag.py" "$harness/wsl_diag.sh" "$harness/resume_locations.py" "$scratch/"
    /usr/bin/python3 -I "$scratch/wsl_diag.py" patch "$candidate" "$scratch"
    chown -R journey:journey "$scratch"
    ;;
prefetch)
    # Necessary staging difference: public, hash-verified client bytes are local
    # before the original-shaped journey. The client itself is not executed.
    candidate=${2:?pristine candidate scripts}
    metadata=${3:?metadata}
    [ "$(id -u)" -ne 0 ] && [ "$(id -un)" = journey ] && [ "$HOME" = "$home" ]
    /usr/bin/python3 -I "$scratch/wsl_diag.py" guard-environment
    install=$HOME/.local/share/claude-multi/install/current
    pin=$("$install/runtime/python/bin/python3" -I "$candidate/journey_fixture.py" client --install "$install")
    set -- $pin
    [ "$#" -eq 3 ]
    [ "$1" = https://downloads.claude.ai/claude-code-releases/2.1.292/linux-x64/claude ]
    curl --proto '=https' --tlsv1.2 -fsSL --connect-timeout 15 --max-time 120 --retry 2 \
        -o "$scratch/claude" "$1"
    chmod 755 "$scratch/claude"
    /usr/bin/python3 -I "$scratch/wsl_diag.py" verify-client "$scratch/claude" "$install" "$metadata"
    "$install/runtime/python/bin/python3" -I "$scratch/resume_locations.py" prepare \
        "$install" "$PWD/../dist" "$scratch" "$metadata"
    ;;
journey)
    metadata=${2:?metadata}
    workspace=${3:?original candidate Windows workspace}
    [ "$(id -u)" -ne 0 ] && [ "$(id -un)" = journey ] && [ "$HOME" = "$home" ]
    /usr/bin/python3 -I "$scratch/wsl_diag.py" hosted-preflight "$metadata" "$workspace"
    # Siblings under this ordinary shell: the observer never launches or
    # reparents the foreground journey, and neither gets a new session here.
    /usr/bin/python3 -I "$scratch/wsl_diag.py" observe "$metadata" \
        >"$scratch/observer.stdout" 2>"$scratch/observer.stderr" &
    observer=$!
    trap 'kill -TERM "$observer" 2>/dev/null || true' EXIT
    code=0
    CM_DIAG_METADATA="$metadata" JOURNEY_CLIENT="$scratch/claude" \
        sh "$scratch/journey.sh" installed 1.1.0 >"$scratch/journey.stdout" 2>"$scratch/journey.stderr" || code=$?
    /usr/bin/python3 -I "$scratch/wsl_diag.py" finish "$metadata" "$code"
    # Bounded end capture before Windows terminates the entire disposable distro.
    for attempt in $(seq 50); do
        [ ! -s "$metadata/observer.json" ] || exit "$code"
        sleep 0.1
    done
    exit 1
    ;;
*) printf '%s\n' 'usage: wsl_diag.sh stage|prefetch|journey' >&2; exit 2 ;;
esac
