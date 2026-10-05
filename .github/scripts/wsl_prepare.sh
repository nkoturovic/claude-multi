#!/bin/sh
# Prepares a new WSL 2 Ubuntu distribution for the Windows journey (CI only,
# run as root inside the distribution, which the runner discards):
#
# - the tools the journey uses: curl, ssh-keygen, openssl and python3;
# - an ordinary user, journey, made the distribution's default user (what
#   the interactive first start asks a person to create);
# - a throwaway certificate authority that the distribution trusts, and a
#   certificate for localhost signed by it, which the journey's loopback
#   https server presents (install.ps1 fetches install.sh over https only).
#   The authority's key is deleted once it has signed; both expire in two
#   days.
#
#   sh .github/scripts/wsl_prepare.sh

set -eu
umask 077

user=journey
apt-get update
apt-get install --yes --no-install-recommends ca-certificates curl openssh-client openssl python3
id "$user" >/dev/null 2>&1 || useradd --create-home --shell /bin/sh "$user"
printf '[user]\ndefault=%s\n' "$user" >>/etc/wsl.conf

tls=/home/$user/tls
mkdir -p "$tls"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 2 \
	-subj '/CN=claude-multi journey loopback CA' -keyout "$work/ca.key" -out "$tls/ca.pem" \
	-addext 'basicConstraints=critical,CA:TRUE' -addext 'keyUsage=critical,keyCertSign'
openssl req -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -subj '/CN=localhost' \
	-keyout "$tls/server.key" -out "$work/server.csr"
printf 'subjectAltName=DNS:localhost,IP:127.0.0.1\nextendedKeyUsage=serverAuth\n' >"$work/ext"
openssl x509 -req -in "$work/server.csr" -CA "$tls/ca.pem" -CAkey "$work/ca.key" -CAserial "$work/ca.srl" \
	-CAcreateserial -days 2 -extfile "$work/ext" -out "$tls/server.pem"
install -m 0644 "$tls/ca.pem" /usr/local/share/ca-certificates/claude-multi-journey.crt
update-ca-certificates
chown -R "$user:$user" "$tls"
echo "wsl_prepare: $user is the default user; localhost's certificate is trusted"
