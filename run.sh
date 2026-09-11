#!/bin/sh
# HDHR3 -> Plex bridge. Edit hdhr3-proxy.conf, then: ./run.sh
set -e
cd "$(dirname "$0")"
. ./hdhr3-proxy.conf
mkdir -p "$DATA_DIR"
[ -n "$SOURCE" ] && MAP="--channelmap $SOURCE" || MAP=""

# Each multiplex is captured to a temporary file and deleted again. On a Pi
# that is pure write wear on the SD card, so use RAM when it is big enough.
if [ -z "$TMPDIR" ] && [ -d /dev/shm ] && [ -w /dev/shm ]; then
    shm_kb=$(df -Pk /dev/shm 2>/dev/null | awk 'NR==2 {print $4}')
    [ -n "$shm_kb" ] && [ "$shm_kb" -gt 262144 ] && export TMPDIR=/dev/shm
fi
# shellcheck disable=SC2086
exec python3 ./hdhr3_proxy.py -d "$DEVICE" $MAP \
     --port "$PORT" --data-dir "$DATA_DIR" $OPTIONS "$@"
