# Terminal Layout Reference

## Overview

On boot, three GNOME terminal windows are launched via `.desktop` files in
`~/.config/autostart/` (deployed by `update.sh`). The layout targets a
**1366×768** display with the LXDE taskbar at the **top** (~45px).

```
┌─ taskbar (45px) ──────────────────────────────────────────────────────────┐
├─ Dashboard (logview) ────────┬─ Journal (journalctl) ────────────────────┤
│                              │  88x16+500+0                               │
│  50x47+0+0                   ├───────────────────────────────────────────┤
│                              │  Simulation (startup)                      │
│                              │  88x20+500+400                             │
└──────────────────────────────┴───────────────────────────────────────────┘
```

---

## Desktop Files & Current Geometries

| Window | File | Geometry |
|--------|------|----------|
| Dashboard | `linux/logview.desktop` | `50x47+0+0` |
| Journal (`journalctl -f`) | `linux/journalctl.desktop` | `88x16+500+0` |
| Simulation (`startup.sh`) | `linux/startup.desktop` | `88x20+500+400` |

---

## Geometry Format

```
COLSxROWS+X+Y
```

- **COLS** — terminal width in character columns
- **ROWS** — terminal height in character rows  
- **X** — pixels from left edge of screen
- **Y** — pixels from top edge of screen (WM places below taskbar if Y=0)

---

## Key Measurements (1366×768 screen)

| Item | Value |
|------|-------|
| Screen resolution | 1366 × 768 px |
| Taskbar height (top) | ~45 px |
| Usable height | ~723 px |
| Approx. font row height | ~20 px/row |
| GNOME terminal chrome (title + menu) | ~48 px |
| Dashboard terminal width (50 cols) | ~430 px |
| Right terminals X start | 500 px |

### Row height calculation
```
terminal_height_px = (rows × row_height_px) + chrome_px
                   = (rows × ~20) + 48
```

### Journal window (88x16+500+0)
- Height: (16 × 20) + 48 = **368 px**
- WM places at y≈45 (below taskbar), ends at y≈413

### Simulation window (88x20+500+400)
- Starts at y=400 (~7px gap below journal)
- Height: (20 × 20) + 48 = **448 px**
- Ends at y≈848 — WM clips to screen bottom if needed

---

## How to Adjust

To change window positions or sizes, edit the three `.desktop` files:

```
linux/logview.desktop
linux/journalctl.desktop
linux/startup.desktop
```

Commit and push — `update.sh` deploys them to `~/.config/autostart/` on
next run. Changes take effect on the **next reboot** (LXDE re-reads autostart
at login).

### Common adjustments

**Journal overlaps simulation** → increase simulation Y (e.g. `+400` → `+420`)

**Gap too large between terminals** → decrease simulation Y

**Terminals overflow right edge** → reduce COLS (e.g. `88` → `80`)

**Terminals too short** → increase ROWS

**Simulation cut off at bottom** → reduce ROWS or decrease Y start

---

## Deployment Path

```
GitHub repo (linux/*.desktop)
    ↓  update.sh (Tier 1/3/4)
~/.config/autostart/*.desktop
    ↓  LXDE session start (reboot)
Running terminal windows
```

`install.sh` deploys `.desktop` files to `/etc/xdg/autostart/` (system-wide,
requires root). `update.sh` deploys to `~/.config/autostart/` (user dir, no
sudo). LXDE reads both; user dir takes precedence.

---

## nm-applet (WiFi tray icon)

`network-manager-gnome` installs `/etc/xdg/autostart/nm-applet.desktop`
which launches one tray icon. A second icon appears if:
- `~/.config/autostart/nm-applet.desktop` also exists (our old deployed copy)
- `lxsession` autostart also has `@nm-applet`

**Fix applied** (`update.sh` → `suppress_nm_applet()`):
1. Removes `~/.config/autostart/nm-applet.desktop`
2. Creates `~/.config/lxsession/LXDE-pi/autostart` without `@nm-applet`

Runs unconditionally on every `update.sh` execution. Takes effect on next reboot.
