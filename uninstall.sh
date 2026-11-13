#!/usr/bin/env bash
# Remove NetworkLimiter components, but preserve the pidnl dependency and saved rules.
set -euo pipefail
if [[ ${EUID} -eq 0 ]]; then
    echo "Run this as your normal desktop user. It uses sudo internally." >&2
    exit 1
fi
echo "Stopping NetworkLimiter service and removing installed components..."
sudo systemctl disable --now networklimiter.service || true
sudo rm -f /etc/systemd/system/networklimiter.service
sudo rm -f /usr/share/applications/networklimiter.desktop
for name in networklimiter networklimiter-daemon networklimiterctl; do
    sudo rm -f "/usr/bin/$name"
done
sudo rm -rf /usr/lib/networklimiter
rm -f "$HOME/.config/autostart/networklimiter.desktop"
sudo systemctl daemon-reload
echo "Uninstalled. Retained /var/lib/networklimiter/state.json and pidnl."
echo "The networklimiter-admin system group and your membership were left unchanged."
echo "If any old pidnl limits remain due to abnormal shutdown, see the upstream pidnl cleanup command."
