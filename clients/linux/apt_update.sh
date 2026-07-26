#!/bin/bash
version=0.01
log="/usr/local/scripts/sim.log"
echo "apt update Script Version $version" | tee -a "$log"
echo "$(date)" | tee -a "$log"
#------------------------------------------------------------
# Ensure dpkg is in a clean state before doing anything
#------------------------------------------------------------
sudo dpkg --configure -a
sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq
#------------------------------------------------------------
# ENSURE the packages the update + sim mechanism REQUIRES are actually INSTALLED.
# The upgrade block below is --only-upgrade, which NEVER installs a missing
# package. coreutils = sha256sum — update.sh's content-hash sync gate; without it
# a client silently falls back to the VERSION-only gate and misses content-only
# changes (the "some clients not getting new scripts/dns_fail.txt" bug). dnsutils
# = dig (the DNS sims' core tool); curl = the script/config fetch; jq used by
# helpers. Plain install (no --only-upgrade) so a missing one is added.
#------------------------------------------------------------
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
  coreutils \
  dnsutils \
  curl \
  jq
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
