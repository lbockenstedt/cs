# WiFi driver matrix — Debian 13 (trixie), kernel 6.12

Reference for `install_wifi_drivers.sh`. Every driver the sim-client image can
need, what it covers, and what it requires to build or run.

**SCOPE: USB dongles.** These sim clients are VMs fed by USB passthrough — they
never see a PCIe or M.2 adapter. PCIe/M.2 rows below are kept for reference only
and are marked; nothing in `install_wifi_drivers.sh` builds a PCIe-only driver.
The practical consequence is that **Wi-Fi 6 is the ceiling** (mt7921u mainline,
or rtl8852au/bu out-of-tree) — 6E and Wi-Fi 7 silicon is M.2 except MT7925.

Trixie ships **kernel 6.12**, which is late enough that several chips that
historically needed an out-of-tree DKMS driver are now **mainline**. Installing
a DKMS driver for a chip the kernel already handles is not neutral — both
drivers claim the same USB ID and which one binds is a race. Check the
"mainline since" column before adding anything out-of-tree.

---

## 1. Mainline drivers — firmware only, no build

These are in kernel 6.12. They need **only** the firmware package; there is
nothing to compile, and no DKMS entry to maintain across kernel upgrades.

| Driver | Gen | Chips | Mainline since | Firmware package | Notes |
|---|---|---|---|---|---|
| `rtl8xxxu` | n/ac | RTL8188CU/CUS, 8192CU, 8723AU, 8192EU (partial) | 4.4 | `firmware-realtek` | Older Realtek USB. 8192EU support incomplete — see §3. |
| `rtw88` (`_8822bu`, `_8821cu`, `_8821au`) | ac | RTL8822BU, RTL8821CU, RTL8811CU | 6.1–6.2 (USB) | `firmware-realtek` | **Supersedes out-of-tree `88x2bu` / `8821cu` on this kernel.** |
| `rtw89` | **6 / 6E / 7** | RTL8852AE (6), 8852BE (6), 8852CE (**6E**), 8922AE (**Wi-Fi 7**) | 5.16 / 6.9 (8922) | `firmware-realtek` | PCIe/M.2 only — no USB parts. |
| `mt7601u` | n | MT7601U | 4.2 | `firmware-misc-nonfree` | |
| `mt76x0u` | ac | MT7610U, MT7630U | 4.15 | `firmware-mediatek` / `firmware-misc-nonfree` | TP-Link Archer T2UH. |
| `mt76x2u` | ac | MT7612U | 4.19 | `firmware-mediatek` | Netgear A6210. |
| `mt7921` / `mt7921u` | **6** | MT7921, MT7921AU (**USB**) | 5.16 | `firmware-mediatek` | The main Wi-Fi 6 **USB** option. |
| `mt7922` | **6E** | MT7922 | 5.18 | `firmware-mediatek` | M.2. |
| `mt7925` / `mt7925u` | **Wi-Fi 7** | MT7925 (be200-class) | 6.7 | `firmware-mediatek` | 802.11be. USB variant exists but is rare. |
| `ath9k` / `ath9k_htc` | n | AR9271, AR7010 | 2.6.36 | `firmware-ath9k-htc` | `ath9k_htc` is the USB variant. Firmware is **free**. |
| `ath10k` | ac | QCA988x, QCA6174 | 3.17 | `firmware-atheros` | PCIe. |
| `ath11k` | **6 / 6E** | QCA6390, WCN6855 (**6E**) | 5.6 | `firmware-atheros` | PCIe/M.2. |
| `ath12k` | **Wi-Fi 7** | QCN9274, WCN7850 | 6.3 | `firmware-atheros` | 802.11be. PCIe/M.2. |
| `iwlwifi` | **6 / 6E / 7** | AX200/AX201 (6), AX210/AX211/AX411 (**6E**), BE200/BE202 (**Wi-Fi 7**) | BE200: 6.6 | `firmware-iwlwifi` | Intel M.2. BE200 needs a recent `firmware-iwlwifi`. |
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
| `rtl8852au` | RTL8852AU, RTL8832AU (**Wi-Fi 6 USB**) | — | `morrownr/rtl8852au` | **Yes** — no mainline USB driver for these. The only realistic way to get Wi-Fi 6 on a Realtek USB dongle. |
| `mt7921au` | MT7921AU (**Wi-Fi 6 USB**) | — | *(none needed)* | **No** — `mt7921u` is mainline since 5.16. Listed so it is not mistaken for a gap. |
| `88x2bu` | RTL8812BU, RTL8822BU | `0bda:b812`, `0bda:b820`, `2357:012d` | `morrownr/88x2bu-20210702` | **No — `rtw88_8822bu` is mainline since 6.1.** Only use if the mainline driver misbehaves, and blacklist one of the two. |
| `8821cu` | RTL8811CU, RTL8821CU | `0bda:c811`, `0bda:c820` | `morrownr/8821cu-20210916` | **No — `rtw88_8821cu` is mainline since 6.2.** Same caveat. |
| `broadcom-sta` (`wl`) | Legacy Broadcom PCIe (BCM4311–4360) | — | Debian `broadcom-sta-dkms` | Only for old laptop chips; conflicts with `b43`/`brcmsmac`. |

---

## 1a. Realtek — full coverage

Realtek is the bulk of this fleet's dongles, and its driver story is the messiest
in Linux: three separate mainline families plus a long tail of USB parts that
have never been upstreamed. Which family claims a chip is not obvious from the
model number.

### Mainline Realtek families

| Driver family | Chips | Bus | Mainline since | Firmware |
|---|---|---|---|---|
| `rtl818x` (`rtl8180`, `rtl8187`) | RTL8180, RTL8185, RTL8187/B | PCI, USB | 2.6.23 | none |
| `rtlwifi` (`rtl8192ce`, `rtl8192cu`, `rtl8192de`, `rtl8192se`, `rtl8188ee`, `rtl8723ae`, `rtl8723be`, `rtl8821ae`) | RTL8188CE/EE, 8192CE/CU/DE/SE, 8723AE/BE, 8812AE, 8821AE | PCIe + **8192cu is USB** | 2.6.38–3.10 | `firmware-realtek` |
| `rtl8xxxu` | RTL8188CU/CUS, 8192CU, 8723AU, 8192EU *(partial)* | USB | 4.4 | `firmware-realtek` |
| `rtw88` | RTL8822BE/CE, 8821CE, 8723DE, **8822BU, 8821CU, 8811CU** | PCIe + **USB since 6.1** | 4.20 / 6.1 (USB) | `firmware-realtek` |
| `rtw89` | RTL8852AE/BE (6), 8852CE (6E), **8922AE (Wi-Fi 7)** | PCIe / M.2 | 5.16 / 6.9 | `firmware-realtek` |
| `rtl8723bs` | RTL8723BS | **SDIO** | 4.12 | `firmware-realtek` |

**`rtl8xxxu` vs `rtlwifi` overlap:** both claim RTL8192CU/8188CU. `rtl8xxxu` is
the newer rewrite and is usually preferred; if a dongle misbehaves, blacklisting
one and forcing the other is a legitimate first move:

```
echo 'blacklist rtl8192cu' > /etc/modprobe.d/prefer-rtl8xxxu.conf
```

### Out-of-tree Realtek USB (no mainline driver)

These are the ones to add as the fleet grows — all common, all cheap, none
upstreamed.

| Driver | Chips | Typical VID:PID | Source | Gen |
|---|---|---|---|---|
| `8812au` | RTL8812AU, RTL8821AU | `0bda:8812`, `2357:011e`, `2001:331e` | `morrownr/8812au-20210629` | ac |
| `8814au` | RTL8814AU | `0bda:8813` | `morrownr/8814au-20210629` | ac |
| `88x2bu` | RTL8812BU, RTL8822BU | `0bda:b812`, `2357:012d` | `morrownr/88x2bu-20210702` | ac *(mainline `rtw88` on 6.12)* |
| `8821cu` | RTL8811CU, RTL8821CU | `0bda:c811`, `0bda:c820` | `morrownr/8821cu-20210916` | ac *(mainline `rtw88` on 6.12)* |
| `8188eu` | RTL8188EUS | `0bda:8179` | `morrownr/8188eu-20210902` | n |
| `rtl8192eu` | RTL8192EU | `0bda:818b` | `Mange/rtl8192eu-linux-driver` | n |
| **`rtl8723du`** | RTL8723DU | `0bda:d723` | `lwfinger/rtl8723du` | n + BT |
| **`rtl8192fu`** | RTL8192FU, RTL8188FU | `0bda:f179`, `0bda:f192` | `kelebek333/rtl8192fu-dkms` | n |
| **`rtl8710bu`** | RTL8710BU, RTL8188GU | `0bda:b711` | `morrownr/rtl8710bu` | n |
| **`rtl8852au`** | RTL8852AU, RTL8832AU | — | `morrownr/rtl8852au` | **Wi-Fi 6** |
| **`rtl8852bu`** | RTL8852BU | — | `morrownr/rtl8852bu` | **Wi-Fi 6** |

Bold = added for future dongles; not currently in the fleet's `usb_config`.

**Realtek SDIO** (`rtl8189fs`, `rtl88x2cs`) is omitted deliberately — SDIO parts
appear on SBCs, never on a USB-passthrough VM.

---

## 2a. Wi-Fi 7 (802.11be) drivers

Every 802.11be driver in mainline Linux. All are in kernel 6.12, so trixie needs
**no out-of-tree driver for Wi-Fi 7** — only the firmware package and, for 6 GHz,
a regulatory database (§2b).

| Driver | Chips | Mainline since | Firmware package | Form factor |
|---|---|---|---|---|
| `iwlwifi` | Intel **BE200**, **BE202**, **BE201**, **BE211** | 6.6 | `firmware-iwlwifi` | M.2 / CNVio2 |
| `ath12k` | Qualcomm **QCN9274**, **WCN7850** | 6.3 | `firmware-atheros` | PCIe / M.2 |
| `mt7925` / `mt7925u` | MediaTek **MT7925**, MT7925AU | 6.7 | `firmware-mediatek` | M.2, **and the only realistic USB path** |
| `rtw89` (`_8922ae`) | Realtek **RTL8922AE** | 6.9 | `firmware-realtek` | PCIe / M.2 |
| `mt7996` | MediaTek **MT7996**, **MT7992**, **MT7990** | 6.4–6.7 | `firmware-mediatek` | AP-class radios, not clients |

Notes that matter more than the version numbers:

- **`mt7925u` is the only Wi-Fi 7 chip with a real USB story.** Everything else
  above is M.2/PCIe. For a fleet fed by USB passthrough, an MT7925-based USB
  adapter is the only way to put Wi-Fi 7 in front of a sim client today.
- **`mt7996` is an AP driver**, listed for completeness — it drives access-point
  radios, not station adapters. It will not help a client VM associate.
- **Firmware recency is the usual failure**, not the driver. BE200 / MT7925 /
  WCN7850 firmware lands in `firmware-*` packages later than the driver lands in
  the kernel, so a current kernel plus a stale firmware package binds the device
  and then fails with "firmware failed to load".
- Mainline-since values are the release where support became usable; some chips
  saw fixes for several releases after. Trixie's 6.12 is comfortably past all of
  them.

---

## 2b. Wi-Fi 6E / Wi-Fi 7 — extra requirements

Beyond the driver, 6 GHz has requirements the 2.4/5 GHz path does not:

| Requirement | Why | Failure mode if missing |
|---|---|---|
| `wireless-regdb` (+ `iw`) | 6 GHz channels are regulatory-gated | Adapter associates fine on 2.4/5 GHz; **every 6 GHz channel is silently absent**. Reads as a hardware limit. |
| Correct regulatory domain | `iw reg set <CC>` — world-roaming permits almost no 6 GHz | Same as above. Check with `iw reg get`. |
| Recent firmware | BE200 / MT7925 / WCN7850 firmware is newer than the driver | Driver binds, then "firmware failed to load"; no interface. |
| Kernel ≥ 6.6 (Wi-Fi 7) | `iwlwifi` BE200 6.6, `mt7925` 6.7, `ath12k` 6.3, `rtw89_8922` 6.9 | Trixie's 6.12 covers all of these. |
| AP must advertise 6 GHz | 6E/7 clients still need a 6 GHz-capable AP + WPA3/SAE | Client works but never uses 6 GHz. |

**For this fleet specifically:** Wi-Fi 6E and Wi-Fi 7 parts are **PCIe/M.2 only**
(`iwlwifi` BE200, `ath12k`, `rtw89_8922`, `mt7922`). There is no meaningful 6E or
Wi-Fi 7 **USB** dongle market yet, so on VMs fed by USB passthrough the practical
ceiling is **Wi-Fi 6** — via `mt7921u` (mainline, nothing to install) or
`rtl8852au` (out-of-tree DKMS). The 6E/7 rows above matter only if a client is
ever given a PCIe/M.2 adapter by passthrough rather than a USB dongle.

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
