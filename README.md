# NetworkLimiter

Native **CachyOS / Arch Linux** per-process download and upload limits. Includes a Qt 6 settings window, KDE Plasma system tray, an optional **global default per-PID cap**, persistent executable overrides, systemd background daemon and automatic desktop startup. **No browser extension.**

> **Status: initial implementation, not yet validated on a CachyOS machine.** The underlying [pidnl](https://github.com/ariasmn/pidnl) backend is a packet **policer**: traffic exceeding a limit is dropped rather than smoothly queued. Actual transfer rates can vary, especially with QUIC/UDP.

## How process rules work

A saved rule applies to **each separate network-owning PID** matching an executable path or process name, not one pooled allowance.

| Target | Rule | Outcome |
|---|---|---|
| All processes (optional default) | Download 100 KiB/s | Each detected network-owning PID gets a separate ~100 KiB/s cap |
| Two independent `curl` processes | `/usr/bin/curl`, download 100 KiB/s | Each process gets its own ~100 KiB/s limit |
| Brave network-service process | Brave executable path, download 100 KiB/s | This network-service process is capped |
| System updater | `/usr/bin/pacman`, download 1024 KiB/s | Root-owned updates capped at ~1 MiB/s; administrator rule required |
| Other processes with no matching rule | None | Unlimited |

**Brave tabs are not separate network processes.** Brave/Chromium generally routes multiple tabs through a shared network-service process. Therefore this app **cannot** independently cap Tab 1 at 100 KiB/s and Tab 2 at 100 KiB/s using only per-PID controls. If both tabs share one network process, they **share the process's total 100 KiB/s cap**. This application does not include or install any browser extension.

**Units:** Rates in the UI are KiB/s (1024 bytes per second). **100 KiB/s ≈ 819 kbit/s**, **1 MiB/s = 1024 KiB/s**. Upload and download limits are independent; either direction can be left unlimited.

## Install on CachyOS / Arch

```bash
git clone https://github.com/Persie0/networklimiter.git
cd networklimiter
bash install.sh
```

Run the installer as your **regular desktop user**, not with `sudo` (it requests sudo when necessary). Currently supports **x86_64**. The installer:

1. Installs Qt, Python and network dependencies from pacman.
2. Installs libcgroup (from configured repositories or using an available `paru`/`yay` AUR helper), if missing.
3. Installs the **SHA-256-verified upstream pidnl 0.1.1 package** if `pidnl` is not installed already.
4. Copies the NetworkLimiter executables and creates/enables `networklimiter.service` at boot.
5. Registers the desktop entry and KDE-compatible XDG login autostart entry.
6. Adds your user to the `networklimiter-admin` group for optional system-wide rules.

**Sign out and back in after installation** to activate new admin group membership. Membership gives permission to set limits for any user's processes, including root, so only give it to trusted users.

Launch `networklimiter` from your app menu or a terminal. Closing the window minimizes to the tray; selecting **Quit tray** does **not** stop the service or release the saved rules.

## Configure rules

**Global default** is disabled by default. In the **Global default** tab, enable the cap and select the maximum download/upload speed for **each** network-owning process, including root system services. This requires administrator group membership and can interfere with system networking. If you only need a few apps limited, leave the global default disabled and define specific rules instead.

In **Persistent rules → Add rule**, choose either an exact executable path or a process name. Select **My processes** (your user) or **All users** (admin group required) and set download/upload caps. Rules persist across logout, application restarts and system reboot. **Executable rules take precedence over the global default.** Set both directions to unlimited in an app rule to exempt it from the global default.

In **Live processes**, you can select a network-active PID to apply or clear a **temporary PID override**. Temporary overrides are lost at service restart or when the process exits. A matching saved rule is automatically reapplied afterward. The service checks newly active network processes approximately every three seconds, so the beginning of a new connection may not be limited.

The tray menu offers **Open**, **Pause my personal rules**, and **Quit tray**. Pausing personal rules also pauses that user's temporary overrides; **it does not disable system rules or the global default**. Administrator controls can pause all system-wide rules and the global default separately.

## Status and diagnostics

```bash
systemctl status networklimiter.service
journalctl -u networklimiter.service -f
networklimiterctl status
networklimiterctl pause
networklimiterctl resume
```

To uninstall:

```bash
bash uninstall.sh
```

Uninstall does not remove the separately installed pidnl package, nor does it delete `/var/lib/networklimiter/state.json` or your desktop user's admin group membership.

## Architecture and limitations

- **GUI:** PySide6/Qt6, themed desktop window and standard system-tray icon. No GUI runs as root.
- **Daemon:** root-owned systemd service with a local Unix control socket; no TCP listening socket or network telemetry.
- **Authorization:** users can change their own per-executable rules and own unprivileged PIDs. Members of `networklimiter-admin` may also set system rules or change root-owned PIDs.
- **Storage:** `/var/lib/networklimiter/state.json`, root-owned, mode 0600; updates use atomic file replacement.
- **Kernel:** pidnl moves network-owning PIDs to cgroups and uses eBPF ingress/egress packet policers; this can affect process grouping managed by systemd. Packet drops can reduce throughput unevenly. Exact speed guarantees are **not** provided. The Linux rate-limit backend is an independent GPL-3.0 project and is not bundled.
- **Matching:** app rules match executable path or process name; independently spawned helper executables may require their own rule. There is no direct browser-tab detection.
- **Lifecycle:** the daemon attempts to remove its owned active PID limits during graceful shutdown. Forced termination or upstream backend failures can leave pinned kernel limits; review pidnl status and cleanup procedures when troubleshooting.

## Development

```bash
python -m unittest discover -s tests -v
```

The unit tests cover rule validation, matching, global defaults, rate units, permissions and daemon rule precedence. Kernel networking, Qt interaction, session startup and the exact upstream pidnl binary still need CachyOS integration testing before declaring a stable release.

This project's own source is licensed under MIT (`LICENSE`). The independently packaged pidnl backend has its own [GPL-3.0 license](https://github.com/ariasmn/pidnl/blob/main/LICENSE).
