#!/bin/bash
version=.04
log="/usr/local/scripts/sim.log"
echo "apt update Script Version $version" | tee -a "$log"
echo "$(date)" | tee -a "$log"
#------------------------------------------------------------
# Ensure dpkg is in a clean state before doing anything
#------------------------------------------------------------
sudo dpkg --configure -a
sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq
#------------------------------------------------------------
# Keep installed packages current
#------------------------------------------------------------
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --only-upgrade \
  linux-headers-$(uname -r) \
  dkms \
  bash \
  coreutils \
  ca-certificates \
  rsyslog \
  network-manager \
  network-manager-gnome \
  wpasupplicant \
  net-tools \
  dnsutils \
  iw \
  wireless-tools \
  rfkill \
  iperf3 \
  git \
  wget \
  smbclient \
  qemu-guest-agent \
  python3 \
  python3-websockets \
  jq \
  cpulimit \
  firefox-esr
#------------------------------------------------------------
# Cleanup
#------------------------------------------------------------
sudo DEBIAN_FRONTEND=noninteractive apt-get autoremove -y
sudo DEBIAN_FRONTEND=noninteractive apt-get autoclean
echo "apt update complete" | tee -a "$log"
