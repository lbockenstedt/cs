echo "Starting DHCP Daemon" | tee -a /usr/scripts/wireless.log
sudo ifconfig enp6s18 up
sudo sleep 15
sudo dhcpcd enp6s18
#--------------------------------------------------------------------------------------------------------
#Setting route metric for wired interface for script update process
sudo ifmetric enp6s18 10
echo "Updating Simulation Script" | tee -a /usr/scripts/wireless.log
sudo wget https://raw.githubusercontent.com/solutions-hpe/client-sim/main/T3/wireless.sh -O /tmp/wireless.sh
sudo wget https://raw.githubusercontent.com/solutions-hpe/client-sim/main/T3/update_script.sh -O /tmp/update_script.sh
#Checking to see if the file downloaded from GitHub is 0 Bytes, if so deleting it as the download failed
sudo find /tmp -type f -size 0 | sudo xargs -r -o rm -v -f
#Moving file to script repo - put in tmp location because if the download fails it overwrites the existing script
sudo mv -f /tmp/wireless.sh /usr/scripts/wireless.sh
sudo mv -f /tmp/update_script.sh /usr/scripts/update_script.sh
#Setting permission to execute the script
sudo chmod 777 /usr/scripts/wireless.sh
sudo chmod 777 /usr/scripts/update_script.sh
#Shutting down wired interface so the simulations are forced out the WLAN
sudo ifconfig enp6s18 down
#--------------------------------------------------------------------------------------------------------
#Removing downloaded files from simulations
rm -r www.*
#--------------------------------------------------------------------------------------------------------
bash /usr/scripts/wireless.sh