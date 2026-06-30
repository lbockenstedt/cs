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
sudo wget https://raw.githubusercontent.com/lbockenstedt/cs/main/clients/t3/wireless.sh -O /tmp/wireless.sh
sudo wget https://raw.githubusercontent.com/lbockenstedt/cs/main/clients/t3/update_script.sh -O /tmp/update_script.sh
sudo wget https://raw.githubusercontent.com/lbockenstedt/cs/main/clients/t3/agent.sh -O /tmp/agent.sh
#Checking to see if the file downloaded from GitHub is 0 Bytes, if so deleting it as the download failed
sudo find /tmp -type f -size 0 | sudo xargs -r -o rm -v -f
#Moving file to script repo - put in tmp location because if the download fails it overwrites the existing script
sudo mv -f /tmp/wireless.sh /usr/scripts/wireless.sh
sudo mv -f /tmp/update_script.sh /usr/scripts/update_script.sh
sudo mv -f /tmp/agent.sh /usr/scripts/agent.sh
#Setting permission to execute the script
sudo chmod 777 /usr/scripts/wireless.sh
sudo chmod 777 /usr/scripts/update_script.sh
sudo chmod 777 /usr/scripts/agent.sh
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
