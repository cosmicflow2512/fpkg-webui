#!/bin/bash
# Local install on Unraid without GitHub/GHCR: builds the image from this folder,
# copies the Unraid template and (re)starts the container.
# Paths can be overridden: SHARES="/mnt/user/NZB:NZB /mnt/user/isos:isos" OUT=... WORK=... ./install.sh
set -euo pipefail
cd "$(dirname "$0")"

NAME=fpkg-webui
IMAGE=${IMAGE:-ghcr.io/cosmicflow2512/fpkg-webui:latest}
PORT=${PORT:-8095}
SHARES=${SHARES:-"/mnt/user/NZB:NZB /mnt/user/download:download"}
OUT=${OUT:-/mnt/user/download/ps5-fpkg/out}
WORK=${WORK:-/mnt/user/download/ps5-fpkg/work}
APPDATA=${APPDATA:-/mnt/user/appdata/fpkg-webui}
TZ=${TZ:-$(cat /etc/timezone 2>/dev/null || echo Europe/Berlin)}

echo "== Image bauen: $IMAGE"
docker build -t "$IMAGE" .

echo "== Unraid-Template ablegen"
TPL=/boot/config/plugins/dockerMan/templates-user
if [ -d /boot/config/plugins/dockerMan ]; then
  mkdir -p "$TPL"
  cp unraid/fpkg-webui.xml "$TPL/my-$NAME.xml"
  echo "   $TPL/my-$NAME.xml"
fi

mkdir -p "$OUT" "$WORK" "$APPDATA"
VOLS=()
for s in $SHARES; do
  host=${s%%:*}; name=${s##*:}
  [ -d "$host" ] || { echo "   übersprungen (fehlt): $host"; continue; }
  VOLS+=(-v "$host:/shares/$name")
done

echo "== Container starten"
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" --restart unless-stopped \
  -p "$PORT":8095 \
  "${VOLS[@]}" \
  -v "$OUT":/output -v "$WORK":/work -v "$APPDATA":/config \
  -e PUID=99 -e PGID=100 -e TZ="$TZ" \
  --label net.unraid.docker.webui="http://[IP]:[PORT:$PORT]/" \
  --label net.unraid.docker.icon="https://raw.githubusercontent.com/cosmicflow2512/fpkg-webui/main/unraid/icon.png" \
  "$IMAGE"

sleep 4
docker logs "$NAME" 2>&1 | tail -5
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
echo "FERTIG: http://${IP:-<server-ip>}:$PORT   (Diagnose-Tab -> Selbsttest)"
