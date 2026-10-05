#!/bin/sh
# claude-multi installer (POSIX sh).
#
#   curl --proto '=https' --tlsv1.2 -fsSL <release URL>/install.sh | sh
#   sh install.sh --from-dir DIR      install from a local release directory
#   sh install.sh --help              every option
#
# It installs one verified bundle into ~/.local/share/claude-multi/install
# (versions/<version>, with `current` and `previous` links), writes small
# launcher files into ~/.local/bin and then starts `claude-multi setup`.
# Nothing outside those places changes, except a PATH line in your shell
# profile when you agree to it, and the claude-multi state directory's
# channel file, which records that this installation owns that state.

set -eu
umask 077

# Release metadata. The release build fills these in for the copy published
# with each release; a copy from the source tree leaves them empty and then
# needs --from-dir or --base-url, and --allowed-signers.
#   RELEASE_VERSION   the version this installer installs by default
#   RELEASE_BASE_URL  https download location; {version} is substituted
#   RELEASE_SIGNERS   allowed_signers lines of the release signing key
#   RELEASE_SUMS      the release's SHA256SUMS, used only when ssh-keygen is missing
RELEASE_VERSION=''
RELEASE_BASE_URL=''
RELEASE_SIGNERS=''
RELEASE_SUMS=''

NAMESPACE='claude-multi-release'
PRINCIPAL='release@claude-multi'
SUMS='SHA256SUMS'
SIG='SHA256SUMS.sshsig'
SHIM_MARK='# claude-multi installer launcher'
PATH_MARK='# added by the claude-multi installer'
SHIMS='claude-multi claude-multi-proxy'
# The bundle's own safety checks (claude_multi.install_txn) and its gateway
# inhibition (claude_multi.gateway_inhibition), each run as `python3 -m
# MODULE` by the bundle's interpreter with its site directory first on
# sys.path.
MODULE_BOOT='import importlib, sys; sys.path.insert(0, sys.argv.pop(1)); raise SystemExit(importlib.import_module(sys.argv.pop(1)).main())'
TXN=claude_multi.install_txn
INHIBITION=claude_multi.gateway_inhibition
REPAIR='sh install.sh --repair'

say() { printf 'claude-multi: %s\n' "$*"; }
warn() { printf 'claude-multi installer: %s\n' "$*" >&2; }
die() { warn "$*"; exit 1; }
usage_error() { warn "$*"; warn "run with --help for the options"; exit 2; }

usage() {
	cat <<'EOF'
Install claude-multi for the current user.

Usage: sh install.sh [options]

  --version VERSION      install this release (default: this installer's release)
  --from-dir DIR         install from a local directory holding the release files
  --base-url URL         download from this https location ({version} is substituted)
  --allowed-signers FILE verify SHA256SUMS against this allowed_signers file instead of
                         the release key (checks this download only; updates keep using
                         the key the installed release carries)
  --modify-path          add ~/.local/bin to PATH in your shell profile
  --no-modify-path       never edit a shell profile (print the line instead)
  --no-setup             do not start `claude-multi setup` afterwards
  --migrate-from-nix     take over a state directory used by a Nix-managed install
  --repair               restore the launcher files and the `current` link; with nothing
                         installed yet, install the release (--from-dir or the download
                         location) to finish an interrupted first installation
  --rollback             switch back to the previously installed version
  --uninstall [-- ARGS]  run `claude-multi uninstall` (it asks before removing anything)
  -y, --yes              accept the default answer to every question
  -h, --help             show this help

Files: ~/.local/share/claude-multi/install (the installed versions and
installer.json, the list of files the installer wrote elsewhere),
~/.local/bin/claude-multi and ~/.local/bin/claude-multi-proxy (launchers).
Linux needs glibc 2.17 or newer. On Windows, run install.ps1 (WSL 2).
EOF
}

# ------------------------------------------------------------------ options

mode=install
version=''
from_dir=''
base_url=''
signers_file=''
modify_path=ask
run_setup=yes
migrate=no
assume_yes=no

need_value() {
	if [ "$#" -lt 2 ] || [ -z "$2" ]; then usage_error "$1 needs a value"; fi
}

while [ "$#" -gt 0 ]; do
	case $1 in
	--version) need_value "$@"; version=$2; shift 2 ;;
	--version=*) version=${1#*=}; shift ;;
	--from-dir) need_value "$@"; from_dir=$2; shift 2 ;;
	--from-dir=*) from_dir=${1#*=}; shift ;;
	--base-url) need_value "$@"; base_url=$2; shift 2 ;;
	--base-url=*) base_url=${1#*=}; shift ;;
	--allowed-signers) need_value "$@"; signers_file=$2; shift 2 ;;
	--allowed-signers=*) signers_file=${1#*=}; shift ;;
	--modify-path) modify_path=yes; shift ;;
	--no-modify-path) modify_path=no; shift ;;
	--no-setup) run_setup=no; shift ;;
	--migrate-from-nix) migrate=yes; shift ;;
	--repair) mode=repair; shift ;;
	--rollback) mode=rollback; shift ;;
	--uninstall) mode=uninstall; shift ;;
	-y | --yes) assume_yes=yes; shift ;;
	-h | --help) usage; exit 0 ;;
	--) shift; break ;;
	*) usage_error "unknown option: $1" ;;
	esac
done
if [ "$#" -gt 0 ] && [ "$mode" != uninstall ]; then
	usage_error "unexpected arguments: $*"
fi

# ------------------------------------------------------------------ paths

case ${HOME:-} in
/*) ;;
*) die "HOME is not set to an absolute path" ;;
esac
case ${XDG_STATE_HOME:-} in
/*) state_base=$XDG_STATE_HOME ;;
*) state_base=$HOME/.local/state ;;
esac
data_home=$HOME/.local/share
install_root=$data_home/claude-multi/install
bin_dir=$HOME/.local/bin
state_root=$state_base/claude-multi
lock_dir=$install_root/.lock
nix_link_legacy=$data_home/claude-multi-release/current
nix_link=$data_home/claude-multi/nix/current

work=''
staging=''
locked=no
txn_bundle=''
inhibit_token=''
# yes once the installation started changing: an interruption from then on
# keeps the gateway inhibition, which `sh install.sh --repair` finishes.
changing=no
# yes when --repair finds nothing installed: the install it runs takes back
# the inhibition an interrupted first installation left.
recovering=no

end_inhibition() {
	if [ -n "$inhibit_token" ] && [ -n "$txn_bundle" ]; then
		bundle_python "$txn_bundle" "$INHIBITION" --state-root "$state_root" end ||
			warn "could not release the gateway inhibition; run 'claude-multi doctor'"
	fi
	inhibit_token=''
	unset CLAUDE_MULTI_INHIBITION_TOKEN
}

cleanup() {
	status=$?
	if [ -n "$inhibit_token" ] && [ "$changing" = yes ]; then
		warn "the installation was interrupted before it finished; gateway changes stay paused until you run: $REPAIR"
		inhibit_token=''
	fi
	end_inhibition
	if [ -n "$work" ]; then rm -rf "$work"; fi
	if [ -n "$staging" ]; then rm -rf "$staging"; fi
	if [ "$locked" = yes ]; then rm -rf "$lock_dir"; fi
	exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# ------------------------------------------------------------------ helpers

quote() {
	printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

tty_available() {
	(exec </dev/tty) 2>/dev/null
}

valid_version() {
	printf '%s\n' "$1" | grep -Eqx '[0-9]+\.[0-9]+\.[0-9]+(-dev)?'
}

sha256_of() {
	if command -v sha256sum >/dev/null 2>&1; then
		sha256sum <"$1" | cut -d ' ' -f 1
	elif command -v shasum >/dev/null 2>&1; then
		shasum -a 256 <"$1" | cut -d ' ' -f 1
	elif command -v openssl >/dev/null 2>&1; then
		openssl dgst -sha256 -r <"$1" | cut -d ' ' -f 1
	else
		die "no sha256 tool found (sha256sum, shasum or openssl)"
	fi
}

link_version() {
	# The version a link in the install root points at (versions/<v>), or nothing.
	if [ -L "$install_root/$1" ]; then
		dest=$(readlink "$install_root/$1" || true)
		case $dest in
		versions/*) printf '%s' "${dest#versions/}" ;;
		esac
	fi
}

runtime_of() {
	printf '%s' "$1/runtime/python/bin/python3"
}

bundle_python() {
	# $1 = a bundle directory, $2 = the module ($TXN or $INHIBITION); the rest
	# = its arguments.
	py_bundle=$1
	shift
	py_site=''
	for candidate in "$py_bundle"/lib/python3*/site-packages; do
		if [ -d "$candidate/claude_multi" ]; then py_site=$candidate; fi
	done
	if [ -z "$py_site" ]; then
		warn "$py_bundle carries no claude_multi package"
		return 1
	fi
	"$(runtime_of "$py_bundle")" -I -B -c "$MODULE_BOOT" "$py_site" "$@"
}

switch_links() {
	# $1 = the version that becomes current, $2 = the one that becomes previous
	# (none: previous stays). One recorded step (install_txn switch): an
	# interruption between the two links is put back by --repair.
	if [ "$#" -ge 2 ]; then
		bundle_python "$txn_bundle" "$TXN" switch --install-root "$install_root" --current "$1" --previous "$2" ||
			exit 1
	else
		bundle_python "$txn_bundle" "$TXN" switch --install-root "$install_root" --current "$1" || exit 1
	fi
}

finish_switch() {
	# Put back the links of a switch an interrupted run left halfway.
	switched=$(bundle_python "$txn_bundle" "$TXN" finish-switch --install-root "$install_root") || exit 1
	if [ -n "$switched" ]; then say "$switched"; fi
}

flip_link() {
	# Atomically point $install_root/$1 at $2 (rename over the old link).
	tmp_link=$install_root/.$1.tmp.$$
	rm -f "$tmp_link"
	ln -s "$2" "$tmp_link"
	"$3" -I -c 'import os, sys; os.replace(sys.argv[1], sys.argv[2])' "$tmp_link" "$install_root/$1" ||
		die "cannot update $install_root/$1"
}

manifest_state_format() {
	# Prints the bundle's state format when MANIFEST.json names this version
	# and target; fails otherwise (also when the bundled runtime cannot run).
	"$1" -I - "$2" "$3" "$4" <<'EOF'
import json, sys
path, version, target = sys.argv[1:4]
try:
    with open(path, encoding="utf-8") as handle:
        doc = json.load(handle)
except (OSError, ValueError):
    sys.exit(1)
fmt = doc.get("state_format")
if (doc.get("name") != "claude-multi" or doc.get("version") != version
        or (target != "*" and doc.get("target") != target)
        or not isinstance(fmt, int) or isinstance(fmt, bool)):
    sys.exit(1)
print(fmt)
EOF
}

mkdir_private() (
	# mkdir -p creates intermediate parents with 0777, even with -m 700.
	# An inherited default ACL can override umask; give every new dir a mode.
	[ -d "$1" ] && exit 0
	parent=${1%/*}
	[ -n "$parent" ] || parent=/
	mkdir_private "$parent" || exit 1
	mkdir -m 700 "$1" 2>/dev/null || [ -d "$1" ] || die "cannot create private directory $1"
)

acquire_lock() {
	mkdir_private "$install_root"
	chmod 700 "$install_root"
	if mkdir -m 700 "$lock_dir" 2>/dev/null; then
		printf '%s\n' "$$" >"$lock_dir/pid"
		locked=yes
		return 0
	fi
	holder=''
	IFS= read -r holder <"$lock_dir/pid" 2>/dev/null || true
	case $holder in
	'' | *[!0-9]*) die "another install or update holds $lock_dir. If none is running, remove that directory and try again." ;;
	*) if kill -0 "$holder" 2>/dev/null; then die "another install or update is running (process $holder)"; fi ;;
	esac
	stale=$install_root/.lock.stale.$$
	if mv "$lock_dir" "$stale" 2>/dev/null; then rm -rf "$stale"; fi
	mkdir -m 700 "$lock_dir" 2>/dev/null || die "another install or update is running"
	printf '%s\n' "$$" >"$lock_dir/pid"
	locked=yes
}

migrate_flag() {
	if [ "$migrate" = yes ]; then printf '%s' --migrate; fi
}

preflight() {
	# $1 = the bundle that runs the checks; the rest = extra preflight options.
	pf_bundle=$1
	shift
	# shellcheck disable=SC2046 # migrate_flag prints nothing or one word
	bundle_python "$pf_bundle" "$TXN" preflight --state-root "$state_root" --install-root "$install_root" \
		$(migrate_flag) "$@" || exit 1
}

hold_inhibition() {
	# The token of this run's inhibition goes to every later child command.
	inhibit_token=$1
	CLAUDE_MULTI_INHIBITION_TOKEN=$inhibit_token
	export CLAUDE_MULTI_INHIBITION_TOKEN
}

begin_transaction() {
	# $1 = the bundle that runs the checks, $2 = what this run does. Records
	# the installer's inhibition of gateway changes (stale once this shell is
	# gone) before the first change.
	txn_bundle=$1
	token=$(bundle_python "$txn_bundle" "$INHIBITION" --state-root "$state_root" begin --owner installer \
		--purpose "$2" --phase prepare --remedy "$REPAIR" --pid "$$") || exit 1
	hold_inhibition "$token"
}

recover_transaction() {
	# --repair: take back the inhibition an interrupted run of the installer
	# left (explicit, under the install lock); fails when there is none.
	txn_bundle=$1
	token=$(bundle_python "$txn_bundle" "$INHIBITION" --state-root "$state_root" recover --owner installer) || exit 1
	[ -n "$token" ] || return 1
	hold_inhibition "$token"
	changing=yes
}

advance() {
	# Record the next phase (it also proves the record is still this run's).
	bundle_python "$txn_bundle" "$INHIBITION" --state-root "$state_root" advance --phase "$1" || exit 1
	case $1 in
	replace | switch | prune | launchers) changing=yes ;;
	esac
}

tidy_interrupted() {
	# What an interrupted install, update or rollback left behind (this run
	# holds the install lock): a version it moved aside comes back when its
	# directory is missing; staging trees, partial downloads and half-made
	# links go.
	for dir in "$install_root"/versions/.replaced.*; do
		[ -d "$dir" ] || continue
		name=${dir##*/.replaced.}
		name=${name%.*}
		if valid_version "$name" && [ ! -e "$install_root/versions/$name" ] && [ ! -L "$install_root/versions/$name" ]; then
			mv "$dir" "$install_root/versions/$name"
			say "put back $name, which an interrupted run had moved aside"
		else
			rm -rf "$dir"
		fi
	done
	rm -rf "$install_root"/versions/.staging.* "$install_root/.downloads"
	rm -f "$install_root"/.current.tmp.* "$install_root"/.previous.tmp.* "$install_root"/.switch.json.tmp.*
}

protected_versions() {
	# The versions a running gateway executes from, one per line; "unknown"
	# when that cannot be checked (then every installed version is kept).
	if found=$(bundle_python "$txn_bundle" "$TXN" protected --state-root "$state_root" --install-root "$install_root"); then
		printf '%s' "$found"
	else
		printf 'unknown'
	fi
}

is_protected() {
	# $1 = version, $2 = the protected_versions output
	[ "$2" = unknown ] && return 0
	printf '%s\n' "$2" | grep -qxF "$1"
}

prune_versions() {
	# Remove installed versions other than current, previous and the ones a
	# running gateway executes from.
	keep_current=$(link_version current)
	keep_previous=$(link_version previous)
	protected=$(protected_versions)
	if [ "$protected" = unknown ]; then
		say "kept every installed version (whether a running gateway uses one cannot be checked)"
		return 0
	fi
	for dir in "$install_root"/versions/*; do
		[ -d "$dir" ] || continue
		name=${dir##*/}
		if [ "$name" = "$keep_current" ] || [ "$name" = "$keep_previous" ]; then continue; fi
		if is_protected "$name" "$protected"; then
			say "kept $name: the running gateway executes it (it goes once that gateway restarts and an install or update runs)"
			continue
		fi
		rm -rf "$dir"
	done
}

prune_copies() {
	# Claude Code copies no installed release needs (a copy a session runs is
	# kept and reported in use).
	bundle_python "$txn_bundle" "$TXN" prune-copies --version "$version" ||
		warn "Claude Code copies were not cleaned up; 'claude-multi setup --step claude' does it later"
}

# ------------------------------------------------------------------ platform

check_libc() {
	if command -v ldd >/dev/null 2>&1 && ldd --version 2>&1 | grep -qi musl; then
		die "this system uses the musl C library; the Linux bundles need glibc 2.17 or newer"
	fi
	if command -v getconf >/dev/null 2>&1; then
		libc=$(getconf GNU_LIBC_VERSION 2>/dev/null || true)
		case $libc in
		'glibc '*)
			libc=${libc#glibc }
			major=${libc%%.*}
			minor=${libc#*.}
			minor=${minor%%.*}
			case $major$minor in
			'' | *[!0-9]*) ;;
			*) if [ "$major" -lt 2 ] || { [ "$major" -eq 2 ] && [ "$minor" -lt 17 ]; }; then
				die "glibc $libc is too old; the Linux bundles need glibc 2.17 or newer"
			fi ;;
			esac
			;;
		esac
	fi
}

mount_type() {
	[ -r /proc/mounts ] || return 0
	awk -v p="$1" '
		{ m = $2; pre = (m == "/") ? "/" : m "/"
		  if ((p == m || index(p, pre) == 1) && length(m) >= best) { best = length(m); t = $3 } }
		END { print t }' /proc/mounts
}

check_wsl_home() {
	home_real=$( (cd "$HOME" 2>/dev/null && pwd -P) || printf '%s' "$HOME")
	case $home_real in
	/mnt/[A-Za-z] | /mnt/[A-Za-z]/*)
		die "your home directory ($home_real) is on a Windows drive. claude-multi needs a Linux home directory inside WSL 2 (for example /home/<you>)."
		;;
	esac
	case $(mount_type "$home_real") in
	drvfs | 9p | v9fs)
		die "your home directory ($home_real) is on a Windows file system (DrvFs). claude-multi needs a Linux home directory inside WSL 2."
		;;
	esac
}

detect_target() {
	os=$(uname -s)
	arch=$(uname -m)
	case $os in
	Linux)
		kernel=$(uname -r 2>/dev/null || true)
		case $kernel in
		*Microsoft*)
			die "WSL 1 is not supported. In Windows, convert the distribution with 'wsl --set-version <distribution> 2' and run the installer again."
			;;
		esac
		case $arch in
		x86_64 | amd64) target=linux-x86_64 ;;
		aarch64 | arm64) target=linux-aarch64 ;;
		*) die "unsupported processor: $arch (Linux bundles exist for x86_64 and aarch64)" ;;
		esac
		check_libc
		case $kernel in
		*microsoft*) check_wsl_home ;;
		esac
		;;
	Darwin)
		case $arch in
		arm64) target=darwin-arm64 ;;
		x86_64)
			if [ "$(sysctl -n sysctl.proc_translated 2>/dev/null || true)" = 1 ]; then
				target=darwin-arm64
			else
				target=darwin-x86_64
			fi
			;;
		*) die "unsupported processor: $arch (macOS bundles exist for arm64 and x86_64)" ;;
		esac
		;;
	MINGW* | MSYS* | CYGWIN* | Windows_NT)
		die "native Windows is not supported. Run install.ps1 in PowerShell: it installs claude-multi inside WSL 2."
		;;
	*)
		die "unsupported operating system: $os (claude-multi runs on Linux, macOS, and Windows through WSL 2)"
		;;
	esac
}

# ------------------------------------------------------------------ other installations

check_links() {
	# The links of another installation (the state root's own channel
	# marker is checked by the bundle, with the runtime's rules).
	if [ -L "$install_root/current" ]; then
		case $(readlink "$install_root/current" || true) in
		/nix/store/*) die "$install_root/current points into /nix/store, so it is not managed by this installer. Remove that link, or keep using the Nix package." ;;
		esac
	fi
	[ "$migrate" = yes ] && return 0
	for link in "$nix_link_legacy" "$nix_link"; do
		if [ -L "$link" ]; then
			case $(readlink "$link" || true) in
			/nix/store/*) die "a Nix-managed claude-multi is installed ($link). Keep updating it through Nix, or run the installer again with --migrate-from-nix to switch this account to the installer." ;;
			esac
		fi
	done
}

claim_state() {
	# shellcheck disable=SC2046 # migrate_flag prints nothing or one word
	replaced=$(bundle_python "$txn_bundle" "$TXN" claim --state-root "$state_root" $(migrate_flag)) || exit 1
	if [ -n "$replaced" ]; then say "this account's claude-multi state now belongs to the installer (it was '$replaced')"; fi
}

# ------------------------------------------------------------------ launchers

check_shim_paths() {
	for name in $SHIMS; do
		shim=$bin_dir/$name
		if [ -e "$shim" ] || [ -L "$shim" ]; then
			if [ -L "$shim" ] || [ ! -f "$shim" ] || ! grep -qF "$SHIM_MARK" "$shim"; then
				die "$shim exists and was not written by this installer. Move it away and run the installer again."
			fi
		fi
	done
}

write_shims() {
	mkdir_private "$bin_dir"
	for name in $SHIMS; do
		shim=$bin_dir/$name
		tmp_shim=$bin_dir/.$name.tmp.$$
		{
			printf '#!/bin/sh\n%s\n' "$SHIM_MARK"
			printf 'exec %s "$@"\n' "$(quote "$install_root/current/bin/$name")"
		} >"$tmp_shim"
		chmod 755 "$tmp_shim"
		mv -f "$tmp_shim" "$shim"
	done
}

on_path() {
	case ":${PATH:-}:" in
	*":$bin_dir:"* | *":$bin_dir/:"*) return 0 ;;
	esac
	return 1
}

profile_file() {
	case ${SHELL:-} in
	*/zsh) printf '%s' "$HOME/.zshrc" ;;
	*/bash) printf '%s' "$HOME/.bashrc" ;;
	*/fish) ;;
	*) printf '%s' "$HOME/.profile" ;;
	esac
}

# shellcheck disable=SC2016 # the profile line expands when the profile runs
export_line='export PATH="$HOME/.local/bin:$PATH"'
path_file=''
handle_path() {
	on_path && return 0
	profile=$(profile_file)
	if [ -z "$profile" ]; then
		# shellcheck disable=SC2088 # a literal ~ in a message
		say "~/.local/bin is not on your PATH. In fish, run: fish_add_path ~/.local/bin"
		return 0
	fi
	decision=$modify_path
	if [ "$decision" = ask ]; then
		if [ "$assume_yes" = yes ]; then
			decision=yes
		elif tty_available; then
			printf 'claude-multi: add ~/.local/bin to PATH in %s? [Y/n] ' "$profile" >/dev/tty
			answer=''
			IFS= read -r answer </dev/tty || true
			case $answer in
			'' | y | Y | yes | YES) decision=yes ;;
			*) decision=no ;;
			esac
		else
			decision=no
		fi
	fi
	if [ "$decision" = yes ]; then
		# One whole line (the receipt records it exactly, so an uninstall
		# removes this line and nothing else).
		if [ ! -f "$profile" ] || ! grep -qxF "$export_line $PATH_MARK" "$profile"; then
			if [ -s "$profile" ] && [ -n "$(tail -c 1 "$profile")" ]; then printf '\n' >>"$profile"; fi
			printf '%s %s\n' "$export_line" "$PATH_MARK" >>"$profile"
		fi
		path_file=$profile
		say "added ~/.local/bin to PATH in $profile; open a new terminal (or run: . $profile)"
	else
		# shellcheck disable=SC2088 # a literal ~ in a message
		say "~/.local/bin is not on your PATH. Add this line to your shell profile: $export_line"
	fi
}

write_receipt() {
	# installer.json lists what this installer created outside the install
	# root (launchers with their sha256, PATH lines exactly as written), so
	# `claude-multi uninstall` can remove exactly that.
	set --
	for name in $SHIMS; do set -- "$@" --launcher "$bin_dir/$name"; done
	if [ -n "$path_file" ]; then set -- "$@" --path-line "$path_file" "$export_line $PATH_MARK"; fi
	bundle_python "$txn_bundle" "$TXN" receipt --install-root "$install_root" "$@" || exit 1
}

hand_off() {
	[ "$run_setup" = yes ] || return 0
	if tty_available; then
		say "starting claude-multi setup"
		"$bin_dir/claude-multi" setup </dev/tty || say "setup did not finish; run 'claude-multi setup' to continue"
	else
		say "next: run 'claude-multi setup'"
	fi
}

# ------------------------------------------------------------------ download and verify

fetch_file() {
	# $1 = release file name, $2 = destination
	if [ -n "$from_dir" ]; then
		[ -f "$from_dir/$1" ] || return 1
		cp "$from_dir/$1" "$2"
		return 0
	fi
	url=$download_base/$1
	if command -v curl >/dev/null 2>&1; then
		curl --proto '=https' --tlsv1.2 -fsSL --retry 3 -o "$2" "$url"
	elif command -v wget >/dev/null 2>&1; then
		wget --https-only -q -O "$2" "$url"
	else
		die "neither curl nor wget is available to download $url"
	fi
}

resolve_source() {
	if [ -n "$from_dir" ]; then
		[ -d "$from_dir" ] || die "--from-dir $from_dir is not a directory"
		from_dir=$(cd "$from_dir" && pwd -P)
		if [ -z "$version" ] && [ -z "$RELEASE_VERSION" ]; then
			found=''
			for file in "$from_dir"/claude-multi-*-"$target".tar.gz; do
				[ -f "$file" ] || continue
				[ -z "$found" ] || die "$from_dir holds several releases; pass --version"
				found=${file##*/}
			done
			[ -n "$found" ] || die "$from_dir has no claude-multi bundle for $target"
			found=${found#claude-multi-}
			version=${found%-"$target".tar.gz}
		fi
	fi
	[ -n "$version" ] || version=$RELEASE_VERSION
	[ -n "$version" ] || usage_error "this copy of the installer names no release: pass --version (and --from-dir or --base-url)"
	valid_version "$version" || usage_error "not a release version: $version"
	if [ -z "$from_dir" ]; then
		base=${base_url:-$RELEASE_BASE_URL}
		[ -n "$base" ] || usage_error "this copy of the installer names no download location: pass --from-dir or --base-url"
		case $base in
		https://*) ;;
		*) die "downloads must use https: $base" ;;
		esac
		download_base=$(printf '%s' "$base" | sed "s/{version}/$version/g")
		download_base=${download_base%/}
	fi
}

verify_download() {
	# A signature is checked whenever ssh-keygen is installed, and then it
	# must verify: a failed or missing signature is never replaced by the
	# checksums built into this installer, which are used only when
	# ssh-keygen is not installed.
	asset=claude-multi-$version-$target.tar.gz
	fetch_file "$SUMS" "$work/$SUMS" || die "the release has no $SUMS"
	signers=''
	if [ -n "$signers_file" ]; then
		[ -f "$signers_file" ] || die "--allowed-signers $signers_file is not a file"
		signers=$signers_file
	elif [ -n "$RELEASE_SIGNERS" ]; then
		printf '%s\n' "$RELEASE_SIGNERS" >"$work/allowed_signers"
		signers=$work/allowed_signers
	fi
	sums_file=''
	if command -v ssh-keygen >/dev/null 2>&1; then
		[ -n "$signers" ] || die "cannot verify the download: this copy of the installer carries no release key. Use the installer published with the release, or pass --allowed-signers FILE."
		fetch_file "$SIG" "$work/$SIG" 2>/dev/null || die "cannot verify the download: the release has no $SIG"
		if ssh-keygen -Y verify -f "$signers" -I "$PRINCIPAL" -n "$NAMESPACE" -s "$work/$SIG" \
			<"$work/$SUMS" >"$work/verify.out" 2>&1; then
			sums_file=$work/$SUMS
			if [ -n "$signers_file" ]; then
				say "verified the $SUMS signature with the key in $signers_file (not the release key)"
			else
				say "verified the $SUMS signature"
			fi
		else
			die "the $SUMS signature does not verify: $(tr '\n' ' ' <"$work/verify.out"); nothing was installed"
		fi
	elif [ -n "$RELEASE_SUMS" ] && [ "$version" = "$RELEASE_VERSION" ]; then
		printf '%s\n' "$RELEASE_SUMS" >"$work/embedded-sums"
		sums_file=$work/embedded-sums
		say "ssh-keygen is not installed: verifying against the checksums built into this installer instead of the signature"
	elif [ -z "$signers" ] && [ -z "$RELEASE_SUMS" ]; then
		die "cannot verify the download: this copy of the installer carries no release key or checksums. Use the installer published with the release, or pass --allowed-signers FILE."
	else
		die "cannot verify the download: ssh-keygen (OpenSSH 8.1 or newer) is not installed. Install OpenSSH, or use the installer published with this release."
	fi
	expected=$(awk -v n="$asset" '$2 == n || $2 == "*" n { print $1 }' "$sums_file")
	printf '%s\n' "$expected" | grep -Eqx '[0-9a-f]{64}' || die "$SUMS has no single entry for $asset"
	fetch_file "$asset" "$work/$asset" || die "the release has no $asset"
	actual=$(sha256_of "$work/$asset")
	[ "$actual" = "$expected" ] || die "checksum mismatch for $asset (expected $expected, got $actual); nothing was installed"
}

unpack_bundle() {
	# Unpack into the private work directory (outside the install root) and
	# check the bundle before anything is changed.
	top=claude-multi-$version-$target
	mkdir -m 700 "$work/unpack"
	(cd "$work/unpack" && tar -xzf "$work/$asset") || die "cannot unpack $asset"
	entries=$(ls -A "$work/unpack")
	if [ "$entries" != "$top" ] || [ ! -d "$work/unpack/$top" ]; then die "$asset does not hold exactly $top/"; fi
	unpacked=$work/unpack/$top
	new_runtime=$(runtime_of "$unpacked")
	[ -x "$new_runtime" ] || die "$asset has no runtime/python/bin/python3"
	new_format=$(manifest_state_format "$new_runtime" "$unpacked/MANIFEST.json" "$version" "$target") ||
		die "the bundled runtime does not run on this system, or its MANIFEST.json does not match $version for $target"
}

# ------------------------------------------------------------------ install

install_bundle() {
	dest=$install_root/versions/$version
	old=$(link_version current)
	if [ -e "$dest" ] || [ -L "$dest" ]; then
		protected=$(protected_versions)
		if is_protected "$version" "$protected"; then
			die "claude-multi $version is installed and the running gateway executes it (or that cannot be checked), so it is not replaced in place. Use --repair to restore the launchers, or restart the gateway (claude-multi gateway restart) after installing another version."
		fi
	fi
	advance replace
	mkdir_private "$install_root/versions"
	staging=$install_root/versions/.staging.$$
	rm -rf "$staging"
	mv "$unpacked" "$staging"
	txn_bundle=$staging
	replaced=''
	if [ -e "$dest" ] || [ -L "$dest" ]; then
		replaced=$install_root/versions/.replaced.$version.$$
		rm -rf "$replaced"
		mv "$dest" "$replaced"
	fi
	mv "$staging" "$dest"
	staging=''
	txn_bundle=$dest
	if [ -n "$replaced" ]; then rm -rf "$replaced"; fi
	advance switch
	if [ -n "$old" ] && [ "$old" != "$version" ] && [ -d "$install_root/versions/$old" ]; then
		switch_links "$version" "$old"
	else
		switch_links "$version"
	fi
	advance prune
	prune_versions
	prune_copies
}

finish_install() {
	advance launchers
	write_shims
	claim_state
	handle_path
	write_receipt
}

do_install() {
	detect_target
	check_links
	check_shim_paths
	resolve_source
	work=$(mktemp -d "${TMPDIR:-/tmp}/claude-multi-install.XXXXXX")
	verify_download
	unpack_bundle
	# Nothing has changed yet; check ownership, inhibitions and the state
	# format, then again under the install lock and this run's inhibition.
	set --
	if [ "$recovering" = yes ]; then set -- --recoverable installer; fi
	preflight "$unpacked" --version "$version" --readable-format "$new_format" --bundle "$unpacked" "$@"
	acquire_lock
	if [ "$recovering" = yes ] && recover_transaction "$unpacked"; then
		say "finishing an interrupted run of the installer"
		tidy_interrupted
		finish_switch
	else
		begin_transaction "$unpacked" "install claude-multi $version"
	fi
	preflight "$unpacked" --version "$version" --readable-format "$new_format" --bundle "$unpacked" --token-env
	install_bundle
	finish_install
	end_inhibition
	say "installed claude-multi $version ($target) in $install_root"
	if [ -n "$old" ] && [ "$old" != "$version" ]; then
		say "kept $old for 'sh install.sh --rollback'; a running gateway keeps the old code until it restarts (claude-multi gateway restart)"
	fi
	hand_off
}

newest_version() {
	for dir in "$install_root"/versions/*; do
		[ -d "$dir" ] || continue
		name=${dir##*/}
		valid_version "$name" && printf '%s\n' "$name"
	done | sort -t . -k 1,1n -k 2,2n -k 3,3n | tail -n 1
}

repair_version() {
	# The version a repair makes current: the one `current` names, else the
	# newest installed one; sets version, bundle, runtime and format. When an
	# interrupted run moved the only one aside, that copy runs the checks
	# (the repair puts it back). Fails when nothing is installed.
	version=$(link_version current)
	if [ -z "$version" ] || [ ! -d "$install_root/versions/$version" ]; then
		version=$(newest_version)
	fi
	bundle=$install_root/versions/$version
	if [ -z "$version" ]; then
		for dir in "$install_root"/versions/.replaced.*; do
			[ -d "$dir" ] || continue
			name=${dir##*/.replaced.}
			name=${name%.*}
			if valid_version "$name"; then
				version=$name
				bundle=$dir
			fi
		done
	fi
	[ -n "$version" ] || return 1
	runtime=$(runtime_of "$bundle")
	format=$(manifest_state_format "$runtime" "$bundle/MANIFEST.json" "$version" "$target") ||
		die "version $version in $install_root is damaged; run the installer again to reinstall it"
}

repair_first_install() {
	# Nothing is installed yet: an interrupted first installation is
	# finished by installing the release again, verified like any install,
	# whose run takes back the inhibition the interrupted one left.
	if [ -z "$from_dir" ] && [ -z "$base_url" ] && [ -z "$RELEASE_BASE_URL" ]; then
		die "nothing is installed in $install_root yet. To finish an interrupted first installation, repair with the release: sh install.sh --repair --from-dir DIR (or --version VERSION --base-url URL); otherwise run the installer without --repair"
	fi
	say "nothing is installed in $install_root yet: installing the release, which finishes an interrupted first installation"
	recovering=yes
	do_install
}

do_repair() {
	detect_target
	check_links
	check_shim_paths
	if ! repair_version; then
		repair_first_install
		return
	fi
	preflight "$bundle" --version "$version" --readable-format "$format" --recoverable installer
	acquire_lock
	# An interrupted install, repair or rollback left its inhibition: this
	# run takes it back and finishes the job; otherwise it records its own.
	if recover_transaction "$bundle"; then
		say "finishing an interrupted run of the installer"
	else
		begin_transaction "$bundle" "repair claude-multi $version"
	fi
	preflight "$bundle" --version "$version" --readable-format "$format" --token-env
	advance switch
	tidy_interrupted
	repair_version || die "nothing is installed in $install_root"  # a version put back may be the one `current` names
	txn_bundle=$bundle
	finish_switch
	repair_version || die "nothing is installed in $install_root"  # the links put back name the one that stays current
	txn_bundle=$bundle
	flip_link current "versions/$version" "$runtime"
	finish_install
	end_inhibition
	say "repaired claude-multi $version"
}

do_rollback() {
	current=$(link_version current)
	previous=$(link_version previous)
	if [ -z "$previous" ] || [ ! -d "$install_root/versions/$previous" ]; then
		die "there is no previous version to roll back to"
	fi
	target_bundle=$install_root/versions/$previous
	runtime=$(runtime_of "$target_bundle")
	format=$(manifest_state_format "$runtime" "$target_bundle/MANIFEST.json" "$previous" '*') ||
		die "the previous version $previous is damaged; reinstall it with --version $previous"
	# The checks run in the newer of the two (the current version), which
	# knows every rule the older one does; a damaged current falls back to
	# the previous version.
	checker=$target_bundle
	if [ -n "$current" ] && [ -d "$install_root/versions/$current" ]; then checker=$install_root/versions/$current; fi
	preflight "$checker" --version "$previous" --readable-format "$format" --bundle "$target_bundle" \
		--rollback-from "${current:-the current version}"
	acquire_lock
	# The rollback is the one checked above: refused when another run changed
	# the links before this one took the lock.
	if [ "$(link_version current)" != "$current" ] || [ "$(link_version previous)" != "$previous" ]; then
		die "the installation changed while the rollback waited for the install lock; nothing was changed. Run it again."
	fi
	begin_transaction "$checker" "roll back claude-multi to $previous"
	preflight "$checker" --version "$previous" --readable-format "$format" --bundle "$target_bundle" \
		--rollback-from "${current:-the current version}" --token-env
	# `previous` becomes the version that was current before this switch
	# (whatever the version numbers), so a second rollback undoes this one.
	advance switch
	if [ -n "$current" ] && [ -d "$install_root/versions/$current" ]; then
		switch_links "$previous" "$current"
	else
		switch_links "$previous"
	fi
	end_inhibition
	say "rolled back to claude-multi $previous${current:+ ($current is kept as the previous version)}"
	say "a running gateway keeps the code it started with until it restarts (claude-multi gateway restart)"
}

do_uninstall() {
	launcher=$install_root/current/bin/claude-multi
	[ -x "$launcher" ] || die "no installed claude-multi in $install_root. Remove that directory and the launchers listed in $install_root/installer.json by hand."
	trap - EXIT
	if tty_available; then
		exec "$launcher" uninstall "$@" </dev/tty
	fi
	exec "$launcher" uninstall "$@"
}

if [ "$(id -u)" = 0 ] && [ -n "${SUDO_USER:-}" ]; then
	die "run the installer as your own user, not through sudo"
fi

case $mode in
install) do_install ;;
repair) do_repair ;;
rollback) do_rollback ;;
uninstall) do_uninstall "$@" ;;
esac
