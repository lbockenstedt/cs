# WiFi driver matrix — Debian 13 (trixie), kernel 6.12

Reference for `install_wifi_drivers.sh`. Every driver the sim-client image can
need, what it covers, and what it requires to build or run.

Trixie ships **kernel 6.12**, which is late enough that several chips that
historically needed an out-of-tree DKMS driver are now **mainline**. Installing
a DKMS driver for a chip the kernel already handles is not neutral — both
drivers claim the same USB ID and which one binds is a race. Check the
"mainline since" column before adding anything out-of-tree.

---

## 1. Mainline drivers — firmware only, no build

These are in kernel 6.12. They need **only** the firmware package; there is
nothing to compile, and no DKMS entry to maintain across kernel upgrades.

| Driver | Chips | Mainline since | Firmware package | Notes |
|---|---|---|---|---|
| `rtl8xxxu` | RTL8188CU/CUS, 8192CU, 8723AU, 8192EU (partial) | 4.4 | `firmware-realtek` | Covers the older Realtek USB parts. 8192EU support is incomplete — see §3. |
| `rtw88` (`_8822bu`, `_8821cu`, `_8821au`) | RTL8822BU, RTL8821CU, RTL8811CU | 6.1–6.2 (USB) | `firmware-realtek` | **Supersedes the out-of-tree `88x2bu` / `8821cu` DKMS drivers on this kernel.** |
| `rtw89` | RTL8852AE/BE, 8922 | 5.16+ | `firmware-realtek` | PCIe parts. |
| `mt7601u` | MT7601U | 4.2 | `firmware-misc-nonfree` | |
| `mt76x0u` | MT7610U, MT7630U | 4.15 | `firmware-mediatek` / `firmware-misc-nonfree` | TP-Link Archer T2UH. |
| `mt76x2u` | MT7612U | 4.19 | `firmware-mediatek` | Netgear A6210. |
| `mt7921u` / `mt7922` | MT7921AU, MT7922 | 5.16 / 5.18 | `firmware-mediatek` | Wi-Fi 6. |
| `ath9k` / `ath9k_htc` | AR9271, AR7010 | 2.6.36 | `firmware-ath9k-htc` | `ath9k_htc` is the USB variant. Firmware is **free**. |
| `ath10k` / `ath11k` | QCA988x, QCA6390, WCN685x | 3.17 / 5.6 | `firmware-atheros` | PCIe. |
| `iwlwifi` | Intel AX200/AX210/BE200 etc. | long-standing | `firmware-iwlwifi` | |
| `brcmfmac` | Broadcom FullMAC (SDIO/USB/PCIe) | long-standing | `firmware-brcm80211` | |
| `rt2800usb` | Ralink RT2770/2870/3070/5370 | 2.6.33 | `firmware-misc-nonfree` | Very common cheap dongles. |
| `rtl8187` | RTL8187/8187B | 2.6.23 | none | |
| `carl9170` | AR9170 | 2.6.37 | `firmware-misc-nonfree` | |
| `zd1211rw` | ZyDAS ZD1211 | 2.6.18 | `firmware-zd1211` | |
| `p54usb` | Prism54 USB | 2.6.25 | `firmware-misc-nonfree` | |
| `ar5523` | Atheros AR5523 | 3.4 | `firmware-misc-nonfree` | |
| `r8152` | RTL8152/8153 **USB Ethernet** | long-standing | none | Not WiFi — `0bda:8153` in the fleet list is a wired dongle. |

**System requirement for all of the above:** the firmware package, and
`non-free-firmware` enabled in apt sources. Nothing else.

---

## 2. Out-of-tree DKMS drivers

Needed only for chips with no usable mainline driver on 6.12.

**Common requirements for every entry here:**

- `dkms`, `build-essential`, `bc`, `libelf-dev`
- `linux-headers-$(uname -r)` — must match the **running** kernel exactly, or the
  build fails with a misleading "kernel source not found"
- `linux-headers-amd64` — so DKMS can rebuild against a *future* kernel
- network access at build time (sources are cloned from GitHub)
- Secure Boot **off**, or the modules must be signed — an unsigned out-of-tree
  module will not load under Secure Boot, and the failure looks like a missing
  driver

| Driver | Chips | Fleet VID:PIDs | Source | Still needed on 6.12? |
|---|---|---|---|---|
| `8812au` | RTL8812AU, RTL8821AU | `0bda:8812`, `2001:331e`, `2357:011e`, `0b05:1a62` | `morrownr/8812au-20210629` | **Yes** — no mainline driver. |
| `8188eu` | RTL8188EUS | `0bda:8179`, `0846:9020` | `morrownr/8188eu-20210902` | **Yes** — the staging `r8188eu` was removed after 6.6. |
| `rtl8192eu` | RTL8192EU | `0bda:818b` | `Mange/rtl8192eu-linux-driver` | **Usually** — `rtl8xxxu` claims it but is unstable on many units. |
| `8814au` | RTL8814AU | — | `morrownr/8814au-20210629` | Yes, if you ever add one. |
| `88x2bu` | RTL8812BU, RTL8822BU | `0bda:b812`, `0bda:b820`, `2357:012d` | `morrownr/88x2bu-20210702` | **No — `rtw88_8822bu` is mainline since 6.1.** Only use if the mainline driver misbehaves, and blacklist one of the two. |
| `8821cu` | RTL8811CU, RTL8821CU | `0bda:c811`, `0bda:c820` | `morrownr/8821cu-20210916` | **No — `rtw88_8821cu` is mainline since 6.2.** Same caveat. |
| `broadcom-sta` (`wl`) | Legacy Broadcom PCIe (BCM4311–4360) | — | Debian `broadcom-sta-dkms` | Only for old laptop chips; conflicts with `b43`/`brcmsmac`. |

---

## 3. Special cases

**`0bda:1a2b` is not a WiFi ID.** It is a Realtek dongle enumerating as a
**CD-ROM** containing Windows drivers. It must be mode-switched before it ever
appears as a network device:

```
usb_modeswitch -v 0bda -p 1a2b -J
```

`usb-modeswitch` + `usb-modeswitch-data` handle this automatically via udev once
installed. Symptom without it: the dongle is visible in `lsusb`, there is no
`wl*` interface, and `dmesg` shows no driver error — because nothing is wrong,
the device just is not a NIC yet.

**Mainline vs DKMS conflict.** If both a mainline driver and a DKMS driver claim
a USB ID, binding is a race and behaviour differs across reboots. Pick one:

```
# prefer the out-of-tree driver
echo 'blacklist rtw88_8822bu' > /etc/modprobe.d/blacklist-rtw88-usb.conf
# or prefer mainline: remove the DKMS module instead
dkms remove -m 88x2bu -v 20210702 --all
```

**Secure Boot.** Any DKMS module must be MOK-signed to load. On these sim VMs
Secure Boot is normally off; if it is on, `modprobe` fails with "Key was
rejected by service" and the chip looks unsupported.

**Kernel upgrades.** DKMS rebuilds automatically *if* `linux-headers-amd64` is
installed. Without it the rebuild is skipped silently and the driver disappears
at the next boot — a slow-motion fleet outage. `install_wifi_drivers.sh`
installs it for this reason.

---

## 4. Verifying a dongle after install

```
lsusb                      # is it enumerated at all?
iw dev                     # did a wl* interface appear?
dmesg | tail -40           # bind errors, firmware-load failures
dkms status                # which out-of-tree modules are built for this kernel
modinfo <driver> | head    # confirm which driver claims it
```

Sequence to interpret:

| Symptom | Meaning |
|---|---|
| Not in `lsusb` | USB passthrough / hardware, not a driver problem |
| In `lsusb`, no `wl*`, no dmesg error | CD-ROM mode — needs `usb_modeswitch` |
| In `lsusb`, dmesg "firmware failed to load" | Missing firmware package, driver is fine |
| In `lsusb`, dmesg "Key was rejected" | Secure Boot rejecting an unsigned DKMS module |
| `wl*` exists, never associates | Driver + firmware fine — this is a config/RF problem |
