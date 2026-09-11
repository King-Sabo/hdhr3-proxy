#!/bin/sh
# Install to /opt/hdhr3 and enable the service. Run as root.
set -e
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }

command -v hdhomerun_config >/dev/null || {
    echo "hdhomerun_config not found. Install it first, e.g.:"
    echo "  Debian/Ubuntu : apt install hdhomerun-config"
    echo "  Fedora        : dnf install libhdhomerun"
    echo "  Arch          : pacman -S libhdhomerun"
    exit 1; }

id hdhr3 >/dev/null 2>&1 || useradd --system --home /var/lib/hdhr3 --shell /usr/sbin/nologin hdhr3
install -d -o hdhr3 -g hdhr3 /var/lib/hdhr3
install -d /opt/hdhr3
install -m 0644 hdhr3_proxy.py hdhr3_epg.py README.md /opt/hdhr3/
install -m 0755 run.sh check.sh /opt/hdhr3/
[ -f /opt/hdhr3/hdhr3.conf ] || install -m 0644 hdhr3.conf /opt/hdhr3/
install -m 0644 hdhr3-proxy.service /etc/systemd/system/

echo
echo "Edit /opt/hdhr3/hdhr3.conf (DEVICE and SOURCE), then:"
echo "  /opt/hdhr3/check.sh"
echo "  systemctl enable --now hdhr3-proxy"
echo "  journalctl -fu hdhr3-proxy"
