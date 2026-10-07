#!/bin/sh
# CI-only disposable WSL distribution. No host-network or host-service fallback.
set -eu
umask 077
home=/home/journey
scratch=$home/diag

case ${1:-} in
stage)
    # Run after the original signed fixture install, before the watchdog.
    harness=${2:?harness scripts}
    candidate=${3:?pristine candidate scripts}
    metadata=${4:?Windows-local metadata through drvfs}
    [ "$(id -u)" -eq 0 ]
    chown journey:journey "$metadata"
    chmod 0777 "$metadata" # drvfs without Linux metadata may retain its mount uid.
    runuser -u journey -- test -w "$metadata" || {
        printf '%s\n' 'Disposable journey user cannot write the explicit metadata directory' >&2
        exit 1
    }
    mkdir "$scratch"
    mkdir /cm-diag-metadata
    cp "$harness/wsl_diag.py" "$harness/wsl_diag.sh" "$scratch/"
    python3 -I "$scratch/wsl_diag.py" patch "$candidate" "$scratch"
    chown -R journey:journey "$scratch"
    # Fetch only public client bytes, using the installed release's contract URL.
    # No client execution, product setup, gateway or provider access here.
    runuser -u journey -- env -i HOME="$home" PATH=/usr/bin:/bin sh -eu -c '
        metadata=$2
        install=$HOME/.local/share/claude-multi/install/current
        pin=$("$install/runtime/python/bin/python3" -I "$1/journey_fixture.py" client --install "$install")
        set -- $pin
        [ "$#" -eq 3 ]
        [ "$1" = https://downloads.claude.ai/claude-code-releases/2.1.292/linux-x64/claude ]
        curl --proto "=https" --tlsv1.2 -fsSL --connect-timeout 15 --max-time 120 --retry 2 \
            -o "$HOME/diag/claude" "$1"
        chmod 755 "$HOME/diag/claude"
        python3 -I "$HOME/diag/wsl_diag.py" verify-client "$HOME/diag/claude" "$install" "$metadata"
    ' sh "$candidate" "$metadata"
    ;;
run)
    [ "$(id -u)" -eq 0 ]
    metadata=${2:?Windows-local metadata through drvfs}
    python3 -I "$scratch/wsl_diag.py" namespace "$metadata" pending
    net=$(readlink /proc/self/ns/net)
    pid=$(readlink /proc/self/ns/pid)
    ipc=$(readlink /proc/self/ns/ipc)
    # Private mount namespace supplies private proc, /tmp and /run. Linux PID 1
    # exiting kills all descendants, including detached gateway/client groups.
    cd "$scratch"
    code=0
    unshare --net --pid --ipc --mount --fork --kill-child=KILL --mount-proc \
        sh "$scratch/wsl_diag.sh" inside "$metadata" "$net" "$pid" "$ipc" >/dev/null 2>&1 || code=$?
    python3 -I "$scratch/wsl_diag.py" namespace "$metadata" "$code"
    if [ "$code" -ne 0 ]; then
        printf '%s\n' 'WSL diagnostic namespace/journey failed; no uncontained retry' >&2
    fi
    exit "$code"
    ;;
inside)
    metadata=${2:?metadata}
    [ "$$" -eq 1 ]
    [ "$(readlink /proc/self/ns/net)" != "$3" ]
    [ "$(readlink /proc/self/ns/pid)" != "$4" ]
    [ "$(readlink /proc/self/ns/ipc)" != "$5" ]
    [ "$(readlink /proc/1/ns/pid)" = "$(readlink /proc/self/ns/pid)" ]
    mount --make-rprivate /
    mount --bind "$metadata" /cm-diag-metadata
    # No Windows-visible product HOME, work/project, binaries or raw output.
    mount -t tmpfs -o mode=0755,nosuid,nodev tmpfs /mnt
    mount -t tmpfs -o mode=0755,nosuid,nodev tmpfs /run
    mount -t tmpfs -o mode=1777,nosuid,nodev tmpfs /tmp
    ip link set dev lo up
    [ "$(ip -o link show | wc -l)" -eq 1 ]
    for path in "$home" "$scratch" "$home/.local/share/claude-multi/install/current"; do
        case $(stat -f -c %T "$path") in ext2/ext3|ext4) ;; *) exit 1 ;; esac
    done
    exec runuser -u journey -- env -i HOME="$home" USER=journey LOGNAME=journey \
        PATH="$home/.local/bin:/usr/bin:/bin" TMPDIR=/tmp CLAUDE_CODE_TMPDIR=/tmp \
        CM_DIAG_METADATA=/cm-diag-metadata JOURNEY_CLIENT="$scratch/claude" \
        python3 -I "$scratch/wsl_diag.py" run "$scratch"
    ;;
*) printf '%s\n' 'usage: wsl_diag.sh stage|run|inside' >&2; exit 2 ;;
esac
