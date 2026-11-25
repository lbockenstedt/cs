#!/bin/bash
scriptver=".02"
echo "Starting DHCP Daemon" | tee -a /usr/scripts/wireless.log
sudo ifconfig enp6s18 up
sudo sleep 15
sudo dhcpcd enp6s18
#--------------------------------------------------------------------------------------------------------
#Setting route metric for wired interface for script update process
sudo ifmetric enp6s18 10
echo "Updating Simulation Script" | tee -a /usr/scripts/wireless.log
# Download into a private temp folder (not fixed /tmp names another user could
# plant), and replace a script only when its download is non-empty. mv swaps
# the inode, so this running script and the agent keep their old copy.
tmpd=$(sudo mktemp -d) || tmpd=""
if [[ -n "$tmpd" ]]; then
  for f in wireless.sh update_script.sh agent.sh; do
    if sudo wget https://raw.githubusercontent.com/lbockenstedt/cs/main/clients/t3/$f -O "$tmpd/$f" && sudo test -s "$tmpd/$f"; then
      sudo chmod 755 "$tmpd/$f"
      sudo mv -f "$tmpd/$f" /usr/scripts/$f
    fi
  done
  sudo rm -rf "$tmpd"
fi
#Setting permission to execute the script (root runs these; never world-writable)
sudo chmod 755 /usr/scripts/wireless.sh
sudo chmod 755 /usr/scripts/update_script.sh
sudo chmod 755 /usr/scripts/agent.sh
sudo chmod -x /etc/udev/rules.d/90-Wireless.rules
#Shutting down wired interface so the simulations are forced out the WLAN
sudo ifconfig enp6s18 down
#--------------------------------------------------------------------------------------------------------
#Removing downloaded files from simulations
rm -r www.*
#Old file cleanup
rm /usr/scripts/dhcpcd.conf
#--------------------------------------------------------------------------------------------------------
bash /usr/scripts/agent.sh >/dev/null 2>&1 || true
bash /usr/scripts/wireless.sh
