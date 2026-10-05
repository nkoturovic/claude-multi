#!/bin/sh
# Fixture-only user journeys on release bundles (CI; never a provider call).
#
#   sh .github/scripts/journey.sh install DIST
#       a fresh install as a new user would make it (install.sh from DIST,
#       verified with an ephemeral key made here), then the installed journey
#   sh .github/scripts/journey.sh installed VERSION
#       the installed journey alone, on the installation already in $HOME
#       (tools/release.py verify-draft installs the signed draft with the
#       release trust first): the version line, the first-run checks of
#       the new installation (doctor --first-run: ready, or not ready only
#       for what a fresh install has not set up yet, providers included:
#       nothing connected yet), the installation channel (plain doctor:
#       "this launcher is the bundle channel"), the pinned Claude Code
#       from a verified local copy ($JOURNEY_CLIENT: a file, or "fetch" to
#       download the installed release's pin), the on-demand gateway
#       lifecycle (setup --step gateway, status, stop) with its loopback
#       confinement, a first managed turn against a fixture provider and
#       its resume (with a client), a foreign listener on the gateway port
#       refused, and an uninstall that keeps the state (claude-multi
#       uninstall --keep-setup --yes on a pseudo-terminal, never --force,
#       once every managed session recorded its end: it removes the
#       launchers and the installed release, and keeps the credentials,
#       which only a typed phrase deletes)
#   sh .github/scripts/journey.sh update KEYDIR DIST_A DIST_B
#       install A, check, update to B, "up to date", roll back to A, the
#       installation channel checked after each step; both releases trust
#       the ephemeral key in KEYDIR (key, allowed_signers), because the
#       release key never enters CI
#   sh .github/scripts/journey.sh service DIST
#       a fresh install, then the supervised gateway service under the
#       user's own systemd user manager (Linux; a disposable user): the
#       first port a new install takes is held by another listener, so the
#       gateway records another; gateway service install, install again
#       (the refresh), the unit's FragmentPath (systemctl --user show) is
#       the rendered file, the unit is active and its main process is the
#       installed release's, the gateway answers on its port, gateway
#       service status; then gateway service uninstall removes the unit
#   sh .github/scripts/journey.sh prepare DIST OUT
#       an ephemeral key (OUT/key) and the release signed with it, with its
#       install.sh (OUT/release): what the Windows journey's install.ps1
#       installs from (its test seam: install.sh over loopback https, the
#       release from this directory)
#
# JOURNEY_GATEWAY=skip and JOURNEY_UNINSTALL=skip leave those steps out
# (the repository's own tests run this script on fake releases that carry
# no gateway). JOURNEY_TARGET=<target> requires a fresh install to install
# that bundle target (linux-aarch64 on the arm64 runner). Each step prints
# "journey: <step>: ok"; any failure stops the journey with a nonzero exit.
#
# Providers are fixtures only: the managed turn's provider is
# journey_fixture.py on 127.0.0.1 (keyless, declared and admitted like any
# provider; the admission's consent is answered on a pseudo-terminal), and
# no other provider has a credential. The confinement checks are negative
# controls with a positive twin each: the gateway answers on 127.0.0.1 but
# not on the host's other addresses, where a listener bound to every
# interface does answer (macOS must have such an address; elsewhere, as in
# a network namespace, the check says it does not apply); and with
# another program listening on the gateway port, starting the gateway fails
# without sending that listener a credential, and a managed launch fails.

set -eu
# Exercise ordinary permissive user environments; product modes must be explicit.
umask 0002

say() { printf 'journey: %s\n' "$*"; }
fail() { printf 'journey: FAILED: %s\n' "$*" >&2; exit 1; }

here=$(CDPATH='' cd "$(dirname "$0")" && pwd)
installer_default=$here/../../packaging/install.sh
fixture=$here/journey_fixture.py
work=$(mktemp -d "${TMPDIR:-/tmp}/claude-multi-journey.XXXXXX")
helpers=''
kill_helper() {
	# The interpreter owns a group, so cleanup also stops its descendants.
	# Kill the pid too: cleanup may run before the interpreter creates the group.
	kill -KILL "$1" 2>/dev/null || true
	kill -s KILL -- "-$1" 2>/dev/null || true
	wait "$1" 2>/dev/null || true
	if [ -n "${JOURNEY_HELPERS_DIR:-}" ]; then
		rm -f "$JOURNEY_HELPERS_DIR/$1"
	fi
}
cleanup() {
	for pid in $helpers; do kill_helper "$pid"; done
	rm -rf "$work"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
cm=$HOME/.local/bin/claude-multi
install_dir=$HOME/.local/share/claude-multi/install/current

make_key() {
	# $1 = directory: key, key.pub, allowed_signers
	mkdir -p "$1"
	ssh-keygen -q -t ed25519 -N '' -C 'ephemeral journey key' -f "$1/key"
	printf 'release@claude-multi %s\n' "$(cut -d ' ' -f 1,2 "$1/key.pub")" >"$1/allowed_signers"
}

signed_copy() {
	# $1 = release directory, $2 = key directory, $3 = destination
	mkdir -p "$3"
	cp "$1"/*.tar.gz "$1/MANIFEST.json" "$1/SHA256SUMS" "$3/"
	ssh-keygen -Y sign -f "$2/key" -n claude-multi-release "$3/SHA256SUMS" >/dev/null 2>&1
	mv "$3/SHA256SUMS.sig" "$3/SHA256SUMS.sshsig"
}

install_from() {
	# $1 = signed release directory, $2 = key directory, $3 = installer
	sh "$3" --from-dir "$1" --allowed-signers "$2/allowed_signers" --no-setup --no-modify-path \
		>"$work/install.txt" 2>&1 || { cat "$work/install.txt" >&2; fail "install.sh from $1"; }
	cat "$work/install.txt"
	[ -x "$cm" ] || fail "no launcher at $cm"
}

manifest_version() {
	sed -n 's/^  "version": "\(.*\)",*$/\1/p' "$1/MANIFEST.json" | head -n 1
}

show_tail() {
	# $1 = file: its last lines on stderr (journey logs carry no secrets)
	[ -f "$1" ] && tail -n 20 "$1" >&2
	return 0
}

fx() {
	# journey_fixture.py under the installed release's own Python
	"$install_dir/runtime/python/bin/python3" -I "$fixture" "$@"
}

background() {
	# $1 = pid file, then fixture arguments. Start the interpreter, not an
	# fx subshell: $! stays the listener's pid across exec, in its own group.
	pidfile=$1
	shift
	"$install_dir/runtime/python/bin/python3" -I -c \
		'import os, sys
from pathlib import Path
registry = os.environ.get("JOURNEY_HELPERS_DIR")
if registry:
    (Path(registry) / str(os.getpid())).touch()
os.setpgrp()
os.execv(sys.executable, [sys.executable, "-I", *sys.argv[1:]])' \
		"$fixture" "$@" &
	echo "$!" >"$pidfile"
	helpers="$helpers $!"
}

stop_helper() {
	# $1 = pid file; wait before a later step reuses the port or uninstalls.
	pid=$(cat "$1")
	kill_helper "$pid"
	remaining=''
	for helper in $helpers; do
		[ "$helper" = "$pid" ] || remaining="$remaining $helper"
	done
	helpers=$remaining
}

wait_for() {
	# $1 = what, then a command that succeeds once it is ready (20 s at most)
	what=$1
	shift
	tries=0
	until "$@"; do
		tries=$((tries + 1))
		[ "$tries" -le 40 ] || fail "$what did not become ready"
		sleep 0.5
	done
}

gateway_port() {
	"$cm" gateway status 2>/dev/null |
		sed -n 's|^  endpoint: http://127\.0\.0\.1:\([0-9][0-9]*\).*|\1|p' | head -n 1
}

reply_number() {
	# $1 = a turn's output: the number of the fixture reply it printed
	sed -n 's/.*fixture reply \([0-9][0-9]*\).*/\1/p' "$1" | head -n 1
}

fetch_client() {
	# the installed release's pinned Claude Code; claude-multi checks its
	# size and sha256 when it copies it
	pin=$(fx client --install "$install_dir") || fail "the installed release names no Claude Code for this host"
	url=${pin%% *}
	curl --proto '=https' --tlsv1.2 -fsSL --retry 3 -o "$work/claude" "$url" ||
		fail "download of the pinned Claude Code"
	chmod 755 "$work/claude"
	JOURNEY_CLIENT=$work/claude
}

confinement() {
	# $1 = the running gateway's port
	fx reach --host 127.0.0.1 --port "$1" || fail "the gateway does not answer on 127.0.0.1:$1"
	addresses=$(fx addresses)
	if [ -z "$addresses" ]; then
		[ "$(uname -s)" != Darwin ] || fail "no non-loopback address to check the gateway's confinement from"
		say "loopback confinement: no non-loopback address here (does not apply)"
		return 0
	fi
	background "$work/open.pid" listen --host 0.0.0.0 --port 0 --port-file "$work/open.port" \
		--log "$work/open.log"
	wait_for "the control listener" test -s "$work/open.port"
	open=$(cat "$work/open.port")
	for address in $addresses; do
		fx reach --host "$address" --port "$open" ||
			fail "control: a listener on every interface does not answer on $address:$open"
		if fx reach --host "$address" --port "$1"; then
			fail "the gateway answers on $address:$1 (it must listen on loopback only)"
		fi
	done
	stop_helper "$work/open.pid"
	say "loopback confinement: ok (refused on $(printf '%s' "$addresses" | tr '\n' ' '); the control listener answered)"
}

managed_turn() {
	background "$work/fixture.pid" serve --port-file "$work/fixture.port" --log "$work/fixture.log"
	wait_for "the fixture provider" test -s "$work/fixture.port"
	base="http://127.0.0.1:$(cat "$work/fixture.port")/v1"
	"$cm" providers add --preset lan-openai-compatible --as journey-fixture --base-url "$base" \
		>"$work/declare.txt" 2>&1 || { show_tail "$work/declare.txt"; fail "providers add (the fixture provider)"; }
	"$cm" models add journey-fixture fixture-model-1 --context 131072 --source operator \
		--as custom-journey-fixture >>"$work/declare.txt" 2>&1 ||
		{ show_tail "$work/declare.txt"; fail "models add (the fixture line)"; }
	fx answer -- "$cm" models admit custom-journey-fixture >"$work/admit.txt" 2>&1 ||
		{ show_tail "$work/admit.txt"; fail "models admit (the fixture line)"; }
	say "fixture provider declared and admitted: ok"
	mkdir "$work/project"
	(cd "$work/project" && "$cm" direct --model custom-journey-fixture -- -p "journey turn one") \
		>"$work/turn1.txt" 2>"$work/turn1.err" </dev/null ||
		{ show_tail "$work/turn1.err"; fail "the first managed turn"; }
	first=$(reply_number "$work/turn1.txt")
	[ -n "$first" ] || { show_tail "$work/turn1.txt"; fail "the first managed turn printed no fixture reply"; }
	say "first managed turn: ok (fixture reply $first)"
	(cd "$work/project" && "$cm" -c -- -p "journey turn two") \
		>"$work/turn2.txt" 2>"$work/turn2.err" </dev/null ||
		{ show_tail "$work/turn2.err"; fail "resuming the managed session"; }
	second=$(reply_number "$work/turn2.txt")
	[ -n "$second" ] || { show_tail "$work/turn2.txt"; fail "the resumed turn printed no fixture reply"; }
	fx carried --log "$work/fixture.log" --reply "$second" --earlier "$first" ||
		fail "the resumed turn did not carry the first turn's reply"
	say "resumed managed session: ok (fixture reply $second carried reply $first)"
	# Keep the provider reachable for the foreign-listener launch's preflight.
}

no_credential_sent() {
	# $1 = what may have sent one, $2 = the listener's port
	if grep -q '"credentials": true' "$work/foreign.log" 2>/dev/null; then
		fail "$1 sent a credential to the listener on port $2"
	fi
}

foreign_listener() {
	# $1 = the stopped gateway's port
	background "$work/foreign.pid" listen --port "$1" --log "$work/foreign.log"
	wait_for "the foreign listener" fx reach --host 127.0.0.1 --port "$1"
	if "$cm" gateway start >"$work/foreign-start.txt" 2>&1; then
		fail "gateway start accepted another program's listener on port $1"
	fi
	grep -q 'no token was sent' "$work/foreign-start.txt" ||
		{ show_tail "$work/foreign-start.txt"; fail "gateway start did not refuse the listener on port $1"; }
	no_credential_sent "gateway start" "$1"
	if [ -n "${JOURNEY_CLIENT:-}" ]; then
		if (cd "$work/project" && "$cm" -c -- -p "journey turn three") >"$work/foreign-turn.txt" 2>&1 </dev/null; then
			fail "a managed launch ran against another program's listener on port $1"
		fi
		grep -q 'the gateway was not started' "$work/foreign-turn.txt" ||
			{ show_tail "$work/foreign-turn.txt"; fail "the managed launch did not refuse the listener on port $1"; }
		no_credential_sent "the managed launch" "$1"
	fi
	stop_helper "$work/foreign.pid"
	say "another program on the gateway port refused: ok"
}

first_run() {
	# doctor --first-run on the new installation must succeed: ready, or not
	# ready for nothing but what a fresh install has not set up yet (the
	# check names, states, details and fixes matched in journey_fixture.py);
	# a nonzero exit for any other reason fails the journey
	result=$(fx first-run --launcher "$cm" --install "$install_dir") || fail "doctor's first run (see above)"
	say "first run: ok, $result"
}

bundle_channel() {
	# $1 = after which step: plain doctor names this launcher's channel as
	# the bundle's; a report exits 0, or 1 for a blocked check, anything
	# else fails
	status=0
	"$cm" doctor >"$work/doctor.txt" 2>&1 || status=$?
	[ "$status" -le 1 ] || { show_tail "$work/doctor.txt"; fail "claude-multi doctor ($1) exited $status"; }
	grep -q 'Installation: this launcher is the bundle channel' "$work/doctor.txt" ||
		{ show_tail "$work/doctor.txt"; fail "claude-multi doctor ($1) does not report the bundle channel"; }
	say "installation channel ($1): bundle"
}

session_ends() {
	# uninstall refuses (without --force, which the journey never passes)
	# while a managed session has not recorded its end; a print-mode turn
	# records it as its hooks finish, so wait for it, 60 s at most
	result=$(fx ended --install "$install_dir" --timeout 60) ||
		fail "a managed session has not recorded its end (named above)"
	say "managed sessions ended: ok ($result)"
}

expect_version() {
	shown=$("$cm" --version) || fail "claude-multi --version"
	case $shown in
	"claude-multi $1 ("*) say "version: $shown" ;;
	*) fail "claude-multi --version printed '$shown', expected version $1" ;;
	esac
}

permission_diagnostics() {
	[ "${JOURNEY_PERMISSIONS:-}" = 1 ] || return 0
	for path in "$HOME" "$HOME/.local" "$HOME/.local/share" "$HOME/.local/share/claude-multi" "$HOME/.local/share/claude-multi/install"; do
		[ ! -e "$path" ] || stat -c '%a %U:%G %n' "$path"
	done
	if command -v getfacl >/dev/null 2>&1; then getfacl -p "$HOME" || true; fi
}

private_directories() {
	fx private-dirs || fail "product state directories are not private"
	say "private state directories under umask 0002: ok"
}

fresh_install() {
	# $1 = release directory; sets version
	installer=$1/install.sh
	[ -f "$installer" ] || installer=$installer_default
	version=$(manifest_version "$1")
	[ -n "$version" ] || fail "$1/MANIFEST.json names no version"
	make_key "$work/key"
	signed_copy "$1" "$work/key" "$work/release"
	permission_diagnostics
	install_from "$work/release" "$work/key" "$installer"
	permission_diagnostics
	private_directories
	if [ -n "${JOURNEY_TARGET:-}" ]; then
		grep -q "^claude-multi: installed claude-multi $version ($JOURNEY_TARGET) " "$work/install.txt" ||
			fail "install.sh did not install the $JOURNEY_TARGET bundle"
		say "bundle target: $JOURNEY_TARGET"
	fi
	say "fresh install: ok"
}

journey_install() {
	dist=${1:?usage: journey.sh install DIST}
	fresh_install "$dist"
	journey_installed "$version"
}

journey_installed() {
	version=${1:?usage: journey.sh installed VERSION}
	[ -x "$cm" ] || fail "no launcher at $cm"
	expect_version "$version"
	first_run
	# after the first run, which must meet the installation untouched
	bundle_channel "installed $version"
	[ "${JOURNEY_CLIENT:-}" != fetch ] || fetch_client
	if [ -n "${JOURNEY_CLIENT:-}" ]; then
		"$cm" setup --step claude --claude-from "$JOURNEY_CLIENT" || fail "setup --step claude --claude-from"
		say "pinned Claude Code from a verified local copy: ok"
		smoke=$("$JOURNEY_CLIENT" --version) || fail "the pinned Claude Code does not run here"
		say "client smoke: $smoke"
	fi
	if [ "${JOURNEY_GATEWAY:-run}" != skip ]; then
		"$cm" setup --step gateway || fail "setup --step gateway"
		"$cm" gateway status || fail "gateway status after the start"
		port=$(gateway_port)
		[ -n "$port" ] || fail "gateway status names no endpoint port"
		confinement "$port"
		if [ -n "${JOURNEY_CLIENT:-}" ]; then
			managed_turn
		else
			say "managed turn: no Claude Code here (set JOURNEY_CLIENT); not run"
		fi
		"$cm" gateway stop || fail "gateway stop"
		"$cm" gateway status >"$work/status.txt" 2>&1 || true
		grep -qi 'not running\|stopped' "$work/status.txt" || fail "the gateway did not stop"
		say "on-demand gateway lifecycle: ok"
		private_directories
		foreign_listener "$port"
		if [ -n "${JOURNEY_CLIENT:-}" ]; then
			stop_helper "$work/fixture.pid"
		fi
	fi
	if [ "${JOURNEY_UNINSTALL:-run}" != skip ]; then
		state=${XDG_STATE_HOME:-$HOME/.local/state}/claude-multi
		release=$(readlink "$install_dir") || fail "no installed release at $install_dir"
		session_ends
		# Uninstall needs a terminal: the fixture's pseudo-terminal confirms
		# nothing (--yes) and keeps the credentials at their question.
		fx answer -- "$cm" uninstall --keep-setup --yes >"$work/uninstall.txt" 2>&1 ||
			{ show_tail "$work/uninstall.txt"; fail "claude-multi uninstall --keep-setup --yes"; }
		grep -q 'claude-multi is uninstalled' "$work/uninstall.txt" ||
			{ show_tail "$work/uninstall.txt"; fail "uninstall did not finish"; }
		[ ! -e "$HOME/.local/bin/claude-multi" ] || fail "the launcher was not removed"
		if [ -e "$install_dir" ] || [ -e "${install_dir%/current}/$release" ]; then
			fail "the installed release was not removed"
		fi
		[ -d "$state" ] || fail "the state ($state) was not kept"
		say "uninstall keeping the state: ok"
	fi
}

journey_update() {
	keys=${1:?usage: journey.sh update KEYDIR DIST_A DIST_B}
	first=${2:?usage: journey.sh update KEYDIR DIST_A DIST_B}
	second=${3:?usage: journey.sh update KEYDIR DIST_A DIST_B}
	installer=$first/install.sh
	[ -f "$installer" ] || installer=$installer_default
	old=$(manifest_version "$first")
	new=$(manifest_version "$second")
	signed_copy "$first" "$keys" "$work/a"
	signed_copy "$second" "$keys" "$work/b"
	install_from "$work/a" "$keys" "$installer"
	expect_version "$old"
	bundle_channel "installed $old"
	"$cm" update --check --from-dir "$work/b" >"$work/check.txt" || fail "update --check"
	grep -q "claude-multi $new is available" "$work/check.txt" || fail "update --check does not offer $new"
	say "update check: ok"
	bundle_channel "update check"
	"$cm" update --from-dir "$work/b" --yes || fail "update to $new"
	expect_version "$new"
	bundle_channel "updated to $new"
	"$cm" update --from-dir "$work/b" >"$work/again.txt" || fail "update at the same version"
	grep -q "is up to date" "$work/again.txt" || fail "an equal version is not reported up to date"
	say "update and up to date: ok"
	bundle_channel "up to date"
	"$cm" update --rollback --yes || fail "update --rollback"
	expect_version "$old"
	say "rollback: ok"
	bundle_channel "rolled back to $old"
}

unit_property() {
	# $1 = unit, $2 = property
	systemctl --user show --property "$2" --value "$1"
}

journey_service() {
	dist=${1:?usage: journey.sh service DIST}
	command -v systemctl >/dev/null 2>&1 || fail "systemctl is not installed"
	systemctl --user show-environment >/dev/null 2>&1 ||
		fail "no user service manager answers (XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-unset})"
	fresh_install "$dist"
	expect_version "$version"
	# The first port a new install takes is held: the gateway records another.
	busy=18317
	background "$work/busy.pid" listen --port "$busy" --log "$work/busy.log"
	wait_for "the listener on port $busy" fx reach --host 127.0.0.1 --port "$busy"
	"$cm" setup --step gateway || fail "setup --step gateway"
	private_directories
	port=$(gateway_port)
	if [ -z "$port" ] || [ "$port" = "$busy" ]; then
		fail "the gateway did not record another port than $busy (${port:-none})"
	fi
	say "alternate port: ok ($port)"
	"$cm" gateway service install || fail "gateway service install"
	"$cm" gateway service install || fail "gateway service install again (the refresh)"
	unit=claude-multi-gateway.service
	rendered=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/$unit
	[ -f "$rendered" ] || fail "no rendered unit at $rendered"
	fragment=$(unit_property "$unit" FragmentPath) || fail "systemctl --user show $unit"
	[ "$fragment" = "$rendered" ] || fail "the manager loads $unit from '$fragment', not $rendered"
	active=$(unit_property "$unit" ActiveState)
	[ "$active" = active ] || { systemctl --user status "$unit" >&2 || true; fail "$unit is $active, not active"; }
	pid=$(unit_property "$unit" MainPID)
	exe=$(readlink "/proc/$pid/exe") || fail "the unit's main process ($pid) is gone"
	case $exe in
	"$HOME/.local/share/claude-multi/install/"*) ;;
	*) fail "the unit runs $exe, not the installed release" ;;
	esac
	fx reach --host 127.0.0.1 --port "$port" || fail "the service's gateway does not answer on 127.0.0.1:$port"
	"$cm" gateway service status >"$work/service.txt" 2>&1 ||
		{ show_tail "$work/service.txt"; fail "gateway service status"; }
	for line in 'gateway service: installed' 'backend: systemd' 'unit: current'; do
		grep -q "$line" "$work/service.txt" || { show_tail "$work/service.txt"; fail "gateway service status lacks '$line'"; }
	done
	say "supervised gateway service: ok ($fragment, $active, main process $exe)"
	"$cm" gateway service uninstall || fail "gateway service uninstall"
	[ ! -e "$rendered" ] || fail "uninstall left $rendered"
	loaded=$(unit_property "$unit" LoadState)
	[ "$loaded" = not-found ] || fail "$unit is still $loaded after the uninstall"
	stop_helper "$work/busy.pid"
	say "gateway service uninstall: ok (the unit is gone)"
}

journey_prepare() {
	dist=${1:?usage: journey.sh prepare DIST OUT}
	out=${2:?usage: journey.sh prepare DIST OUT}
	[ -f "$dist/install.sh" ] || fail "$dist has no install.sh"
	make_key "$out/key"
	signed_copy "$dist" "$out/key" "$out/release"
	cp "$dist/install.sh" "$out/release/install.sh"
	say "signed release with its key in $out: ok"
}

mode=${1:-}
[ "$#" -gt 0 ] && shift
case $mode in
install) journey_install "$@" ;;
installed) journey_installed "$@" ;;
update) journey_update "$@" ;;
service) journey_service "$@" ;;
prepare) journey_prepare "$@" ;;
*) fail "usage: journey.sh install DIST | installed VERSION | update KEYDIR DIST_A DIST_B | service DIST | prepare DIST OUT" ;;
esac
say "done"
