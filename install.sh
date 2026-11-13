#!/usr/bin/env bash
# Installer for CachyOS / Arch Linux. Run as your desktop user, not via sudo.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ ${EUID} -eq 0 ]]; then
    echo "Run ./install.sh as your normal desktop user (it invokes sudo itself)." >&2
    exit 1
fi
if [[ ! -f /etc/arch-release ]] || ! command -v pacman >/dev/null; then
    echo "This installer supports Arch Linux and CachyOS only." >&2
    exit 1
fi
if [[ "$(uname -m)" != "x86_64" ]]; then
    echo "Prebuilt pidnl backend requires x86_64." >&2
    exit 1
fi

echo "Installing Qt 6 GUI and daemon dependencies..."
sudo pacman -S --needed python pyside6 python-psutil libbpf elfutils curl

if ! command -v pidnl >/dev/null 2>&1; then
    if ! pacman -Q libcgroup >/dev/null 2>&1; then
        echo "pidnl requires libcgroup (often available through the AUR)."
        if pacman -Si libcgroup >/dev/null 2>&1; then
            sudo pacman -S --needed libcgroup
        elif command -v paru >/dev/null 2>&1; then
            paru -S --needed libcgroup
        elif command -v yay >/dev/null 2>&1; then
            yay -S --needed libcgroup
        else
            echo "Install 'libcgroup' from AUR with paru/yay and rerun ./install.sh." >&2
            exit 1
        fi
    fi

    echo "Installing pinned upstream pidnl 0.1.1 from a SHA-256 verified release..."
    temp="$(mktemp -d)"
    trap 'rm -rf "$temp"' EXIT
    asset="pidnl-0.1.1-1-x86_64.pkg.tar.zst"
    curl --fail --location --retry 2 --proto '=https' --tlsv1.2 \
        "https://github.com/ariasmn/pidnl/releases/download/v0.1.1/$asset" \
        --output "$temp/$asset"
    echo "456d81fde0f7923f59b72b128a19a4d347735829d580dee32a0d79cb0b6e9ff6  $temp/$asset" |
        sha256sum --check --status
    sudo pacman -U --needed "$temp/$asset"
fi

sudo install -d -m 0755 /usr/lib/networklimiter
sudo cp -r -- "$ROOT/networklimiter" /usr/lib/networklimiter/
sudo find /usr/lib/networklimiter/networklimiter -type d -exec chmod 0755 {} +
sudo find /usr/lib/networklimiter/networklimiter -type f -exec chmod 0644 {} +
for name in networklimiter networklimiter-daemon networklimiterctl; do
    sudo install -m 0755 "$ROOT/bin/$name" "/usr/bin/$name"
done
sudo install -m 0644 "$ROOT/packaging/networklimiter.service" /etc/systemd/system/networklimiter.service
sudo install -m 0644 "$ROOT/packaging/networklimiter.desktop" /usr/share/applications/networklimiter.desktop

if ! getent group networklimiter-admin >/dev/null; then
    sudo groupadd --system networklimiter-admin
fi
if ! id -nG "$USER" | grep -qw networklimiter-admin; then
    sudo usermod --append --groups networklimiter-admin "$USER"
    echo "Added $USER to networklimiter-admin. Sign out and in to enable system-wide rules."
fi

mkdir -p "$HOME/.config/autostart"
cp "$ROOT/packaging/networklimiter.desktop" "$HOME/.config/autostart/networklimiter.desktop"

sudo systemctl daemon-reload
sudo systemctl enable --now networklimiter.service

echo
echo "Installed. Launch from your app menu or run: networklimiter"
echo "The background service starts at boot; the tray starts at next login."
echo "Status: systemctl status networklimiter.service"
echo "Note: this uses packet policing, not perfectly smooth traffic shaping."
