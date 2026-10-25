#!/usr/bin/env bash
# Creates a self-signed TLS certificate for the HTTP transport.
# Usage: scripts/make-cert.sh [IP-or-hostname ...]   (default: hostname, localhost and this host's IPs)
set -euo pipefail
cd "$(dirname "$0")/.."
names=("$@")
[ ${#names[@]} -gt 0 ] || names=("$(hostname)" localhost $(hostname -I 2>/dev/null))
san=""
for n in "${names[@]}"; do
  if [[ $n =~ ^[0-9.]+$ || $n == *:* ]]; then san+="IP:$n,"; else san+="DNS:$n,"; fi
done
mkdir -p certs && chmod 700 certs
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 730 \
  -keyout certs/server.key -out certs/server.crt \
  -subj "/CN=${names[0]}" -addext "subjectAltName=${san%,}"
chmod 600 certs/server.key
echo "Certificate: $PWD/certs/server.crt (SHA256: $(openssl x509 -in certs/server.crt -noout -fingerprint -sha256 | cut -d= -f2))"
