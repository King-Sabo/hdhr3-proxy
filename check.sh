#!/bin/sh
# Diagnostic report: does it see the tuner, the lineup, the guide?
set -e
cd "$(dirname "$0")"
. ./hdhr3-proxy.conf
mkdir -p "$DATA_DIR"
[ -n "$SOURCE" ] && MAP="--channelmap $SOURCE" || MAP=""
# shellcheck disable=SC2086
python3 ./hdhr3_proxy.py -d "$DEVICE" $MAP \
     --port "$PORT" --data-dir "$DATA_DIR" $OPTIONS --check "$@"

echo
echo "--- environment ---"
command -v hdhomerun_config >/dev/null && echo "hdhomerun_config: $(command -v hdhomerun_config)" \
    || echo "hdhomerun_config: NOT FOUND  (apt install hdhomerun-config)"
lsmod 2>/dev/null | grep -q dvbhdhomerun && \
    echo "WARNING: dvbhdhomerun kernel module is loaded and will hold the tuners." \
    || true
rmem=$(cat /proc/sys/net/core/rmem_max 2>/dev/null || echo 0)
[ "$rmem" -lt 2097152 ] && \
    echo "note: net.core.rmem_max is $rmem; raise to 4194304 if HD streams stutter." \
    || true
shm_kb=$(df -Pk /dev/shm 2>/dev/null | awk 'NR==2 {print $4}')
if [ -n "$shm_kb" ] && [ "$shm_kb" -gt 262144 ]; then
    echo "temp captures: /dev/shm (${shm_kb}K free) -- kept off the SD card"
else
    echo "temp captures: disk (/dev/shm too small); on an SD card this is"
    echo "               avoidable write wear -- see --data-dir and TMPDIR"
fi
data_dev=$(df -P "$DATA_DIR" 2>/dev/null | awk 'NR==2 {print $1}')
case "$data_dev" in
    *mmcblk*) echo "WARNING: $DATA_DIR is on the SD card ($data_dev)."
              echo "         For a long-running Pi, point DATA_DIR at a USB SSD."
              ;;
esac
