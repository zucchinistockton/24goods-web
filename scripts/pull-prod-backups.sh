#!/usr/bin/env bash
# Copy the newest backup set that production (c4) made by itself to this box and verify it.
# Read-only on c4: it never runs or replaces the backup script there (scripts/prod.sh backup does,
# and refuses until the v2 prerequisites of #28 exist, which left this box without copies).
# Cron: 15 5 * * * cd /root/02project/24goods_web && scripts/pull-prod-backups.sh >> /root/kesiv-backups/pull.log 2>&1
set -euo pipefail
HOST="${PROD_HOST:-141.98.198.150}"; PORT="${PROD_SSH_PORT:-2204}"; RUSER="${PROD_SSH_USER:-root}"
SECRETS="${PROD_SECRETS_DIR:-/root/.secrets}"; DEST="${PROD_LOCAL_BACKUPS:-/root/kesiv-backups}"
REMOTE=/opt/kesiv/backups
KEEP="${KEEP_SETS:-14}"
opts=(-o StrictHostKeyChecking=accept-new -o LogLevel=ERROR)
rsh() { sshpass -f "$SECRETS/c4_ssh_pw" ssh -p "$PORT" "${opts[@]}" "$RUSER@$HOST" "$@"; }

stamp=$(rsh "ls -1 $REMOTE | sed -n 's/^goods24-\\([0-9TZ]*\\)\\.dump\$/\\1/p' | sort | tail -1")
[[ "$stamp" =~ ^[0-9]{8}T[0-9]{6}Z$ ]] || { echo "$(date -u +%FT%TZ) FAIL no backup set found on $HOST"; exit 1; }
out="$DEST/nightly-$stamp"
if [[ -s "$out/goods24-$stamp.dump" ]]; then echo "$(date -u +%FT%TZ) ok already have $stamp"; exit 0; fi
mkdir -p "$out"; chmod 700 "$out"
for f in "goods24-$stamp.dump" "uploads-$stamp.tgz" "config-$stamp.tgz"; do
  sshpass -f "$SECRETS/c4_ssh_pw" scp -P "$PORT" "${opts[@]}" "$RUSER@$HOST:$REMOTE/$f" "$out/$f"
  chmod 600 "$out/$f"
done
want=$(rsh "sha256sum $REMOTE/goods24-$stamp.dump" | cut -d' ' -f1)
have=$(sha256sum "$out/goods24-$stamp.dump" | cut -d' ' -f1)
[[ "$want" == "$have" ]] || { echo "$(date -u +%FT%TZ) FAIL checksum mismatch for $stamp"; exit 1; }
# keep the newest $KEEP nightly sets (pre-deploy sets are never pruned here)
ls -1d "$DEST"/nightly-* 2>/dev/null | sort | head -n -"$KEEP" | while read -r old; do rm -rf -- "$old"; done
echo "$(date -u +%FT%TZ) ok pulled $stamp ($(stat -c %s "$out/goods24-$stamp.dump") bytes, sha256 ${have:0:16})"
