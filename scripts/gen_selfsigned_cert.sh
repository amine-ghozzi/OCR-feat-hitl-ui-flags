#!/usr/bin/env bash
# Generate a self-signed TLS certificate for the single public entrypoint (SEC-7).
#
#   scripts/gen_selfsigned_cert.sh <host> [days]
#
# <host> is the name clients will type — the on-prem server's hostname or IP,
# e.g. `finalze.bank.internal` or `10.1.2.3`. It becomes the CN *and* a SAN
# entry; `localhost`, `127.0.0.1` and `::1` are always added as SANs so the
# smoke test and any SSH-tunnelled client work against the same certificate.
#
# Writes (mode 0600 for the key):
#   certs/server.crt   → point TRAEFIK_CERT_FILE at it
#   certs/server.key   → point TRAEFIK_KEY_FILE at it
# `certs/` is gitignored — never commit a key, self-signed or not.
#
# A bank-provided certificate replaces this step entirely: drop the PEM cert and
# key anywhere on the host and point the SAME two variables at them. Nothing in
# docker-compose.yml or traefik/dynamic.yml changes. If it arrives as a leaf plus a
# separate intermediate/CA file, concatenate them LEAF FIRST into one PEM
# (`cat leaf.crt intermediate.crt > server.crt`) and point TRAEFIK_CERT_FILE at
# that — traefik serves an incomplete chain with no error, and every client that
# does not already hold the intermediate rejects it.
#
# On the server, `certs/` must be 700 and the key 600, owned by the user that runs
# docker; this script sets the file modes but cannot fix a world-readable parent.
set -euo pipefail

HOST="${1:-}"
DAYS="${2:-825}"   # 825 = the maximum most clients still accept for a leaf cert

if [ -z "$HOST" ]; then
    echo "usage: $0 <host-or-ip> [days]" >&2
    echo "  e.g. $0 finalze.bank.internal" >&2
    echo "       $0 10.1.2.3 365" >&2
    exit 2
fi

command -v openssl >/dev/null 2>&1 || { echo "error: openssl not found on PATH" >&2; exit 3; }

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="${CERT_DIR:-$REPO_ROOT/certs}"
CRT="$OUT_DIR/server.crt"
KEY="$OUT_DIR/server.key"

mkdir -p "$OUT_DIR"

if [ -e "$CRT" ] || [ -e "$KEY" ]; then
    echo "error: $OUT_DIR already holds a certificate; remove it first if you mean to replace it" >&2
    exit 4
fi

# An IP literal must appear as IP:…, a name as DNS:… — get it wrong and every
# client rejects the certificate with a SAN mismatch.
if printf '%s' "$HOST" | grep -Eq '^[0-9]+(\.[0-9]+){3}$'; then
    HOST_SAN="IP:$HOST"
else
    HOST_SAN="DNS:$HOST"
fi
SAN="$HOST_SAN,DNS:localhost,IP:127.0.0.1,IP:::1"

openssl req -x509 -newkey rsa:4096 -sha256 -nodes \
    -days "$DAYS" \
    -keyout "$KEY" \
    -out "$CRT" \
    -subj "/CN=$HOST/O=FinAlze/OU=on-prem" \
    -addext "subjectAltName=$SAN" \
    -addext "basicConstraints=critical,CA:FALSE" \
    -addext "keyUsage=critical,digitalSignature,keyEncipherment" \
    -addext "extendedKeyUsage=serverAuth"

chmod 600 "$KEY"
chmod 644 "$CRT"

cat <<EOF

Self-signed certificate written (valid $DAYS days, SAN: $SAN):
  $CRT
  $KEY

Add to .env:
  TRAEFIK_CERT_FILE=$CRT
  TRAEFIK_KEY_FILE=$KEY

Clients must either trust $CRT explicitly or pass an insecure flag
(curl -k, scripts/smoke_compose_ui.py --insecure). Replace it with the
bank-provided certificate by repointing the same two variables.
EOF
