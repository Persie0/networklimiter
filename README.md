# NetworkLimiter — global per-process speed limit

A simple **CachyOS / Arch Linux** network limiter with a KDE Plasma-friendly Qt window and system tray.

**Just two controls:**
1. **Enable speed limit** — turn the global limit on or off.
2. **Maximum download speed per process** — enter a number and select KiB/s or MiB/s.

Settings save automatically. There are **no per-application lists, PID controls, exceptions, upload settings, or browser extensions**.

## What "global per-process" means

The same limit is applied **independently to each detected network-owning Linux PID**. This is **not** a limit on the combined speed of your entire computer.

For example, set the limit to **1 MiB/s**:

| Network-owning process | Approx. maximum download |
|---|---:|
| Browser's network-service process | 1 MiB/s |
| System updater (`pacman`) | 1 MiB/s |
| A separate download process | 1 MiB/s |
| Another separate download process | 1 MiB/s |

Each process has its own allowance. **Upload is unlimited.**

**Brave/Chromium caveat:** tabs often share one network-service process. Two tabs sharing the same PID share its download limit, rather than each having a separate allowance. Independent per-tab limits are not supported without browser integration, which is intentionally not included.

Internally, 1 MiB/s = 1024 KiB/s = 1,048,576 bytes/s. The UI can also be set to e.g. 100 KiB/s. Decimal **MB/s** and binary **MiB/s** are different units.

## Install on CachyOS

```bash
git clone https://github.com/Persie0/networklimiter.git
cd networklimiter
bash install.sh
```

Run the installer **as your desktop user**, without `sudo`; it requests sudo when required. Currently targets **x86_64**. It installs official Qt/Python dependencies and a SHA-256-verified upstream `pidnl` package if needed. `libcgroup` might require an installed AUR helper (`paru` or `yay`).

The installer enables the background systemd service at boot, registers a desktop entry, and enables tray autostart on login. It adds your account to the local `networklimiter-admin` group to allow changing the system-wide limit.

**Sign out and sign back in once after installation** for the system-wide toggle to become editable. The group is privileged: its members can modify bandwidth restrictions on system/root processes. Do not grant it to untrusted users.

Run `networklimiter` from your application menu or terminal. Closing the window keeps it in the system tray. The tray's **Enable speed limit** checkbox controls the **same global setting**, not a separate per-user limit. Quitting the tray does not stop the system service or erase settings.

## Optional terminal controls

```bash
networklimiterctl status
networklimiterctl enable 1024   # 1024 KiB/s download per PID
networklimiterctl disable       # Unlimited downloads again
```

## Updating existing installations

```bash
cd networklimiter
git pull --ff-only
bash install.sh
```

The previous version offered separate user/PID/executable rules. **These are now intentionally unsupported.** On the next daemon start, any old saved app-specific rules are ignored; previously journaled PID limits are reconciled and removed or replaced with the single global limit.

A previous enabled global download setting is migrated when possible. Otherwise the limiter starts **disabled**, with 1 MiB/s selected by default.

## Limitations and implementation

The background service observes network-active processes at approximately one-second intervals and applies the same speed to each detected PID. Brief network bursts may happen before detection. The underlying GPLv3 [pidnl](https://github.com/ariasmn/pidnl) backend uses Linux cgroup v2 and eBPF ingress packet policing. This **drops** excess packets rather than smoothly queuing them. Actual transfers can be uneven (particularly QUIC/UDP), so values are approximate rather than exact physical speed guarantees. cgroup moves can affect process grouping.

The Qt6 GUI runs unprivileged; the root-owned systemd service exposes only **status** and **set_global** on a local Unix socket. Writes are authorized using the peer's process credentials and membership of `networklimiter-admin`. No outbound telemetry is collected.

State is stored at `/var/lib/networklimiter/state.json`, mode 0600. It includes an applied-PID journal for recovery after unexpected restarts. The service attempts to release its limits when stopped normally.

For diagnostics:

```bash
systemctl status networklimiter.service
journalctl -u networklimiter.service -f
python -m unittest discover -s tests -v
```

For removal run `bash uninstall.sh`. This leaves the separately installed pidnl package, system group membership, and saved settings in place.

**Status:** Initial implementation; full kernel and KDE integration still needs testing on an actual CachyOS machine.

## License

The code in this repository is MIT licensed. The separately installed [pidnl backend](https://github.com/ariasmn/pidnl) is GPL-3.0.
