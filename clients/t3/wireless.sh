#!/bin/bash
scriptver=".80"
generic_opt55="1,3,6"
mercury_opt60="dhcpcd-5.5.6:Mercury-6.99.5:i386:i386"
liftmstr_opt60="dhcpcd-5.5.6:busybox-6.99.5:i386:i386"
brightsg_opt60="BrightSign-XT1144"
crestron_opt60="freebsd-kernel:3.9.1"
tesla_opt60="tesla-busybox-1.9.9"
sonos_opt60="dhcpcd-5.5.6:SONOS-6.99.5:i386:i386"
play_signage_opt60="dhcpcd-5.5.6:linux-6.99.5:i386:i386"
polycom_opt60="dhcpcd-5.2.10:Linux-2.6.37+:armv7l:ti8168evm"
zebra_opt60="Zebra-Technologies-ZTC-ZT230-203dpi-ZPL"
zebra_opt55="1,3,44,6,15,12,11"
ring_opt60="busybox"
wepresent_opt60="udhcp-0.9.9-pre"
barco_opt60="Barco-linux-bsd:6.9.9"
resideo_opt60="bsd-kernel:6.9.9"
density_opt60="Density-Device"
hpprint_opt60="Hewlett-Packard_JetDirect"
oculus_opt60="android-dhcp-12"
axis_opt60="Axis,DomeCamera,P3265-LV,10.12.165"
moxa_opt60="udhcp-1.19.2"
moxs_opt55="1,3,4,12,15,28,42"
brightsn_opt55="1,3,6,12,15,26,28,33,42,43,51,58,59,119,121"
#--------------------------------------------------------------------------------------------------------
sleep 10
echo Updating Scripts | tee -a /usr/scripts/wireless.log
github=raw.githubusercontent.com
ping -c1 $github
  if [ $? -eq 0 ]; then
   echo Successful network connection to Github - updating scripts
   sudo wget --waitretry=10 --read-timeout=20 --timeout=15 https://raw.githubusercontent.com/lbockenstedt/cs/main/clients/t3/wireless.sh -O /usr/local/scripts/wireless.sh
   sleep 1
   sudo wget --waitretry=10 --read-timeout=20 --timeout=15 https://raw.githubusercontent.com/lbockenstedt/cs/main/clients/t3/update_script.sh -O /usr/local/scripts/update_script.sh
   sleep 1
   sudo wget --waitretry=10 --read-timeout=20 --timeout=15 https://raw.githubusercontent.com/lbockenstedt/cs/main/clients/t3/agent.sh -O /usr/local/scripts/agent.sh
   sleep 1
fi
sudo chmod 777 /usr/scripts/wireless.sh
sudo chmod 777 /usr/scripts/update_script.sh
sudo chmod 777 /usr/scripts/agent.sh
#--------------------------------------------------------------------------------------------------------
echo "Script Version " $scriptver | tee -a /usr/scripts/wireless.log
echo "Starting DHCP Daemon" | tee -a /usr/scripts/wireless.log
sudo dhcpcd --inactive
#System level changes - checking at every start
#Disable SNAP
echo "Disabling SNAP" | tee -a /usr/scripts/wireless.log
sudo snap refresh --hold
#Scheduling Reboot in 10 hrs
echo "Scheduling Reboot" | tee -a /usr/scripts/wireless.log
sudo shutdown -r 600
#Shutting down the MDNS responder
echo "Shutting down MDNS responder" | tee -a /usr/scripts/wireless.log
sudo systemctl stop avahi-daemon.socket avahi-daemon.service
sudo systemctl disable avahi-daemon.socket avahi-daemon.service
#Disabling IPV6 system wide
echo "Disabling IPV6" | tee -a /usr/scripts/wireless.log
sudo sysctl --write net.ipv6.conf.all.disable_ipv6=1
echo 'blacklist ipv6' | sudo tee '/etc/modprobe.d/blacklist.local' >/dev/null
#Disabling parent WLAN Adapter for simulation
sudo ifconfig wlp6s16 down
#Clearing out any WPA supplicant configuration
#echo "Killing WPA Supplicant" | tee -a /usr/scripts/wireless.log
#sudo pkill wpa_supplicant
#sleep 30
echo "Starting WPA Supplicant" | tee -a /usr/scripts/wireless.log
#--------------------------------------------------------------------------------------------------------
#Loop to Shutdown Adapters
#--------------------------------------------------------------------------------------------------------
for (( h = 1; h <= 9; h++ ))
 do
  echo "Starting WPA Supplicant"
  sudo wpa_supplicant -c /etc/wpa.conf -B -i vwlan$h
  sudo wpa_supplicant -c /etc/wpa.conf -B -i vwlan1$h
 done
#--------------------------------------------------------------------------------------------------------
#echo "Waiting for network connection" | tee -a /usr/scripts/wireless.log
echo "Sleeping for 1 minute" | tee -a /usr/scripts/wireless.log
sleep 60
#--------------------------------------------------------------------------------------------------------
echo "Starting DHCP Simulation"
#vwlan1
sudo dhcpcd -h MercurySD -i $mercury_opt60 -o $generic_opt55 vwlan1
#vwlan2
sudo dhcpcd -h LiftMaster -i $liftmstr_opt60 -o $generic_opt55 vwlan2
#vwlan3
sudo dhcpcd -h BrightSign -i $brightsg_opt60 -o $brightsn_opt55 vwlan3
#vwlan4
sudo dhcpcd -h ZebraPrinter -i $zebra_opt60 -o $zebra_opt55 vwlan4
#vwlan5
sudo dhcpcd -h PlaySignage -i $play_signage_opt60 -o $generic_opt55 vwlan5
#vwlan6
sudo dhcpcd -h SONOS -i $sonos_opt60 -o $generic_opt55 vwlan6
#vwlan7
sudo dhcpcd -h HPPrinter -i $hpprint_opt60 -o $generic_opt55 vwlan7
#vwlan8
sudo dhcpcd -h PolyCom -i $polycom_opt60 -o $generic_opt55 vwlan8
#vwlan9
sudo dhcpcd -h AxisCam -i $axis_opt60 -o $generic_opt55 vwlan9
#vwlan11
sudo dhcpcd -h MoxaDevice -i $moxa_opt60 -o $moxa_opt55 vwlan11
#vwlan12
sudo dhcpcd -h Ring -i $ring_opt60 -o $generic_opt55 vwlan12
#vwlan13
sudo dhcpcd -h Resideo -i $resideo_opt60 -o $generic_opt55 vwlan13
#vwlan14
sudo dhcpcd -h BarcoShare -i $barco_opt60 -o $generic_opt55 vwlan14
#vwlan15
sudo dhcpcd -h WePresentGW -i $wepresent_opt60 -o $generic_opt55 vwlan15
#vwlan16
sudo dhcpcd -h Desnity -i $density_opt60 -o $generic_opt55 vwlan16
#vwlan17
sudo dhcpcd -h Oculus -i $oculus_opt60 -o $generic_opt55 vwlan17
#vwlan18
sudo dhcpcd -h Tesla -i $tesla_opt60 -o $generic_opt55 vwlan18
#vwlan19
sudo dhcpcd -h Crestron -i $crestron_opt60 -o $generic_opt55 vwlan19
#Variable for how long to wait for HTTP/HTTPS timeout in wget command
httpretry=1
#Variable for how many recursive directories WGET attempts to download
httpdepth=1
#Variable for how long to wait for IP address after running dhcpcd command before running simulation
dhcpsleep=15
#--------------------------------------------------------------------------------------------------------
   active=$((RANDOM%19+1))
   active=5
   #Generate a random number to select a random interface to bring online
   echo "Script Version " $scriptver | tee /usr/scripts/wireless.log
   echo "Active WLAN Interface " vlwan$active | tee -a /usr/scripts/wireless.log
   echo "Set random interface" | tee -a /usr/scripts/wireless.log 
   ip a | grep NO-CARRIER
   ip route | grep "metric 20"
   sudo dhcpcd -k vwlan$active
   sleep $dhcpsleep
#--------------------------------------------------------------------------------------------------------  
for (( h = 1; h <= 9; h++ ))
 do
   echo "Running Simulations" | tee -a /usr/scripts/wireless.log
   #Loop to run tests
   case "$active" in
    1)
      sudo dhcpcd -h MercurySD -i $mercury_opt60 -o $generic_opt55 -m 20 vwlan1
      echo "Waiting for network connection" | tee -a /usr/scripts/wireless.log
      sleep $dhcpsleep
      sudo ifmetric vwlan$active 20
      echo "Running MercurySD Simulations" | tee -a /usr/scripts/wireless.log
      pkill -f firefox
      sleep 5
      firefox --headless https://www.mercurysd.ie/products &
      sleep 5
     ;;
    2)
      sudo dhcpcd -h Liftmaster -i $liftmstr_opt60 -o $generic_opt55 -m 20 vwlan2
      echo "Waiting for network connection" | tee -a /usr/scripts/wireless.log
      sleep $dhcpsleep
      sudo ifmetric vwlan$active 20
      echo "Running LiftMaster Simulations" | tee -a /usr/scripts/wireless.log
      dig connect-ca.myqdevice.com
      curl --insecure -o /tmp/liftmaster.file https://40.117.182.120:8883
      curl --insecure -o /tmp/liftmaster.file https://connect-ca.myqdevice.com:8883
      wget -r -t $httpretry -l $httpdepth -np --delete-after --random-wait -e robots=off -k https://www.liftmaster.com/
      pkill -f firefox
      sleep 5
      firefox --headless https://setup.myqdevice.com &
      sleep 5
     ;;
    3)
      sudo dhcpcd -h BrightSign -i $brightsg_opt60 -o $brightsn_opt55 -m 20 vwlan3
      echo "Waiting for network connection" | tee -a /usr/scripts/wireless.log
      sleep $dhcpsleep
      sudo ifmetric vwlan$active 20
      echo "Running BrightSign Simulations" | tee -a /usr/scripts/wireless.log
      pkill -f firefox
      sleep 5
      firefox --headless https://www.brightsign.biz/contact-us/ &
      sleep 5
     ;;
    4)
      sudo dhcpcd -h ZebraPrinter -i $zebra_opt60 -o $zebra_opt55 -m 20 vwlan4
      echo "Waiting for network connection" | tee -a /usr/scripts/wireless.log
      sleep $dhcpsleep
      sudo ifmetric vwlan$active 20
      echo "Running Zebra Simulations" | tee -a /usr/scripts/wireless.log
      pkill -f firefox
      sleep 5
      firefox --headless https://www.zebra.com/us/en.html &
      sleep 5
      #wget -r -t $httpretry -l $httpdepth -np --delete-after --random-wait -e robots=off -k https://www.apple.com/
     ;;
    5)
      sudo dhcpcd -h PlaySignage -i $play_signage_opt60 -o $generic_opt55 -m 20 vwlan5
      echo "Waiting for network connection" | tee -a /usr/scripts/wireless.log
      sleep $dhcpsleep
      sudo ifmetric vwlan$active 20
      echo "Running PlaySinage Simulations" | tee -a /usr/scripts/wireless.log
      pkill -f firefox
      sleep 5
      firefox --headless https://my.playsignage.com &
      sleep 5
      firefox --headless https://us-storage.playsignage.com &
      sleep 5
      firefox --headless https://stream.playsignage.com &
      sleep 5
      firefox --headless https://release.playsignage.com &
      sleep 5
      firefox --headless https://playsignage.com &
      sleep 5
      wget -r -t $httpretry -l $httpdepth -np --delete-after --random-wait -e robots=off -k https://connect.raspberrypi.com/
      wget -r -t $httpretry -l $httpdepth -np --delete-after --random-wait -e robots=off -k https://playsignage.com/
      wget -r -t $httpretry -l $httpdepth -np --delete-after --random-wait -e robots=off -k https://my.playsignage.com/
      wget -r -t $httpretry -l $httpdepth -np --delete-after --random-wait -e robots=off -k https://us-storage.playsignage.com/
      wget -r -t $httpretry -l $httpdepth -np --delete-after --random-wait -e robots=off -k https://stream.playsignage.com/
      wget -r -t $httpretry -l $httpdepth -np --delete-after --random-wait -e robots=off -k https://release.playsignage.com/
      sudo apt update
      sleep 60
     ;;
    6)
      sudo dhcpcd -h SONOS -i $sonos_opt60 -o $generic_opt55 -m 20 vwlan6
      echo "Waiting for network connection" | tee -a /usr/scripts/wireless.log
      sleep $dhcpsleep
      sudo ifmetric vwlan$active 20
      echo "Running SONOS Simulations" | tee -a /usr/scripts/wireless.log
      pkill -f firefox
      sleep 5
      dig onn-i-09007be6d9db10869-us-east-1.lechmere.prod.ws.sonos.com
      dig feature-config.sslauth.sonos.com
      firefox --headless https://conn-i-09007be6d9db10869-us-east-1.lechmere.prod.ws.sonos.com &
      sleep 5
      firefox --headless https://www.sonos.com/en-us/home &
      sleep 5
      curl --insecure -o /tmp/sonos.file https://conn-i-09007be6d9db10869-us-east-1.lechmere.prod.ws.sonos.com
      curl -o /tmp/sonos.file https://feature-config.sslauth.sonos.com
      ping -c 600 feature-config.sslauth.sonos.com
      wget -r -t $httpretry -l $httpdepth -np --delete-after --random-wait -e robots=off -k https://www.sonos.com/
     ;;
    7)
      sudo dhcpcd -h HPPrinter -i $hpprint_opt60 -o $generic_opt55 -m 20 vwlan7
      echo "Waiting for network connection" | tee -a /usr/scripts/wireless.log
      sleep $dhcpsleep
      sudo ifmetric vwlan$active 20
      echo "Running HPPrinter Simulations" | tee -a /usr/scripts/wireless.log
      pkill -f firefox
      sleep 5
      firefox --headless https://support.hp.com/us-en/printer &
      sleep 5
     ;;
    8)
      sudo dhcpcd -h PolyCom -i $polycom_opt60 -o $generic_opt55 -m 20 vwlan8
      echo "Waiting for network connection" | tee -a /usr/scripts/wireless.log
      sleep $dhcpsleep
      sudo ifmetric vwlan$active 20
      echo "Running PolycomIPPhone Simulations" | tee -a /usr/scripts/wireless.log
      pkill -f firefox
      #wget -r -l $httpwait -np --delete-after --random-wait -e robots=off -k https://www.hp.com/us-en/poly.html
     ;;
    9)
      sudo dhcpcd -h AxisCam -i $axis_opt60 -o $generic_opt55 -m 20 vwlan9
      echo "Waiting for network connection" | tee -a /usr/scripts/wireless.log
      sleep $dhcpsleep
      sudo ifmetric vwlan$active 20
      echo "Running AxisNetCam Simulations" | tee -a /usr/scripts/wireless.log
      pkill -f firefox
     ;;
    11)
      sudo dhcpcd -h MoxaDevice -i $moxa_opt60 -o $moxa_opt55 -m 20 vwlan11
      echo "Waiting for network connection" | tee -a /usr/scripts/wireless.log
      sleep $dhcpsleep
      sudo ifmetric vwlan$active 20
      echo "Running MoxaDevice Simulations" | tee -a /usr/scripts/wireless.log
      pkill -f firefox
      #wget -r -t $httpretry -l $httpdepth -np --delete-after --random-wait -e robots=off -k https://www.apple.com/
     ;;
    12)
      sudo dhcpcd -h Ring -i $ring_opt60 -o $generic_opt55 -m 20 vwlan12
      echo "Waiting for network connection" | tee -a /usr/scripts/wireless.log
      sleep $dhcpsleep
      sudo ifmetric vwlan$active 20
      echo "Running Ring Simulations" | tee -a /usr/scripts/wireless.log
      pkill -f firefox
      sleep 5
      dig ec2-3-235-249-68.prd.rings.solutions
      dig api.prod.signalling.ring.devices.a2z.com
      dig alexa.na.gateway.devices.a2z.com
      dig spectrum.s3.amazonaws.com
      dig api.amazon.com
      dig amzn-sidewalk-events-us-east-1-prod.s3.amazonaws.com
      dig stickupcammini.devices.prod.rss.ring.amazon.dev
      dig fw-eventstream.ring.com
      firefox --headless https://api.prod.signalling.ring.devices.a2z.com &
      sleep 5
      firefox --headless https://ring.com
      sleep 5
      curl -o /tmp/ring.file https://api.prod.signalling.ring.devices.a2z.com
      curl -o /tmp/ring.file https://alexa.na.gateway.devices.a2z.com
      curl -o /tmp/ring.file http://spectrum.s3.amazonaws.com
      curl -o /tmp/ring.file https://api.amazon.comonaws.com
      curl -o /tmp/ring.file https://api.amazon.com
      curl -o /tmp/ring.file https://amzn-sidewalk-events-us-east-1-prod.s3.amazonaws.com
      curl -o /tmp/ring.file https://fw-eventstream.ring.com
      wget -r -t $httpretry -l $httpdepth -np --delete-after --random-wait -e robots=off -k https://ring.com/
      ;;
    13)
      sudo dhcpcd -h Resideo -i $resideo_opt60 -o $generic_opt55 -m 20 vwlan13
      echo "Waiting for network connection" | tee -a /usr/scripts/wireless.log
      sleep $dhcpsleep
      sudo ifmetric vwlan$active 20
      echo "Running Resideo Simulations" | tee -a /usr/scripts/wireless.log
      pkill -f firefox
      sleep 5
      firefox --headless https://www.resideo.com/us/en/
      sleep 5
      dig lcc-prodsf-lcc03sf-iothub.azure-devices.net
      dig weather02.clouddevice.io
      curl -o /tmp/resideo.file http://lcc-prodsf-lcc03sf-iothub.azure-devices.net:5671
      curl -o /tmp/resideo.file https://weather02.clouddevice.io
      wget -r -t $httpretry -l $httpdepth -np --delete-after --random-wait -e robots=off -k https://www.resideo.com/
     ;;
    14)
      sudo dhcpcd -h BarcoShare -i $barco_opt60 -o $generic_opt55 -m 20 vwlan14
      echo "Waiting for network connection" | tee -a /usr/scripts/wireless.log
      sleep $dhcpsleep
      sudo ifmetric vwlan$active 20
      echo "Running BarcoShare Simulations" | tee -a /usr/scripts/wireless.log
      pkill -f firefox
      sleep 5
      firefox --headless https://www.barco.com/en/ &
      sleep 5
     ;;
    15)
      sudo dhcpcd -h WePresentGW -i $wepresent_opt60 -o $generic_opt55 -m 20 vwlan15
      echo "Waiting for network connection" | tee -a /usr/scripts/wireless.log
      sleep $dhcpsleep
      sudo ifmetric vwlan$active 20
      echo "Running WePresentGW Simulations" | tee -a /usr/scripts/wireless.log
      pkill -f firefox
     ;;
    16)
      sudo dhcpcd -h Desnity -i $density_opt60 -o $generic_opt55 -m 20 vwlan16
      echo "Waiting for network connection" | tee -a /usr/scripts/wireless.log
      sleep $dhcpsleep
      sudo ifmetric vwlan$active 20
      echo "Running DensitySensor Simulations" | tee -a /usr/scripts/wireless.log
      pkill -f firefox
      sleep 5
      firefox --headless https://www.density.io &
      sleep 5
     ;;
    17)
      sudo dhcpcd -h Oculus -i $oculus_opt60 -o $generic_opt55 -m 20 vwlan17
      echo "Waiting for network connection" | tee -a /usr/scripts/wireless.log
      sleep $dhcpsleep
      sudo ifmetric vwlan$active 20
      echo "Running OculusVR Simulations" | tee -a /usr/scripts/wireless.log
      pkill -f firefox
      wget -r -t $httpretry -l $httpdepth -np --delete-after --random-wait -e robots=off -k https://www.meta.com/
      sleep 5
      firefox --headless https://www.meta.com/quest/ &
      sleep 5
     ;;
    18)
      sudo dhcpcd -h Tesla -i $tesla_opt60 -o $generic_opt55 -m 20 vwlan18
      echo "Waiting for network connection" | tee -a /usr/scripts/wireless.log
      sleep $dhcpsleep
      sudo ifmetric vwlan$active 20
      echo "Running Tesla Simulations" | tee -a /usr/scripts/wireless.log
      pkill -f firefox
      curl --insecure -o /tmp/tesla.file https://x3-prod.obs.tesla.com
      curl --insecure -o /tmp/tesla.file https://hermes-prd.ap.tesla.services
      curl --insecure -o /tmp/tesla.file https://maps-prd.go.tesla.services
      curl --insecure -o /tmp/tesla.file https://telemetry-prd.vn.tesla.services
      #wget -r -t $httpretry -l $httpdepth -np --delete-after --random-wait -e robots=off -k https://www.tesla.com/
     ;;
    19)
      sudo dhcpcd -h Crestron -i $crestron_opt60 -o $generic_opt55 -m 20 vwlan19
      echo "Waiting for network connection" | tee -a /usr/scripts/wireless.log
      sleep $dhcpsleep
      sudo ifmetric vwlan$active 20
      echo "Running Crestron Simulations" | tee -a /usr/scripts/wireless.log
      pkill -f firefox
      sleep 5
      firefox --headless https://www.crestron.com &
      sleep 5
      dig api.my.crestron.com
      dig fc.crestron.io
      wget -r -t $httpretry -l $httpdepth -np --delete-after --random-wait -e robots=off -k https://www.crestron.com/
     ;;
  esac
#--------------------------------------------------------------------------------------------------------     
#Toggling NTP to force an update
#--------------------------------------------------------------------------------------------------------
  echo "Toggling NTP" | tee -a /usr/scripts/wireless.log
  sudo timedatectl set-ntp off
  sudo timedatectl set-ntp on
  echo "Generating DNS Traffic &" | tee -a /usr/scripts/wireless.log
  echo "ICMP for Central Stats" | tee -a /usr/scripts/wireless.log
  for (( i = 1; i <= 9; i++ ))
   do
   #Generating DNS Traffic for stats in Central
   nslookup www.microsoft.com 8.8.8.8 
   nslookup www.nbc.com 1.1.1.1
   nslookup www.aol.com 9.9.9.9
   nslookup www.apple.com 10.0.0.10
   nslookup www.hpe.com 10.0.0.10
   nslookup www.vmware.com 10.0.0.10
  done
#--------------------------------------------------------------------------------------------------------
#End of Loop
#--------------------------------------------------------------------------------------------------------
#Sleeping to keep clients associated
echo "Sleeping for 5 minutes" | tee -a /usr/scripts/wireless.log
sleep 300
done
#--------------------------------------------------------------------------------------------------------
#Resetting DHCP Status on all interfaces
#--------------------------------------------------------------------------------------------------------
 sudo dhcpcd -k vwlan$active
 sleep 2
 sudo dhcpcd -x
 sleep 2
 sudo dhcpcd --inactive
 sleep 2
sudo dhcpcd -h MercurySD -i $mercury_opt60 -o $generic_opt55 vwlan1
sudo dhcpcd -h LiftMaster -i $liftmstr_opt60 -o $generic_opt55 vwlan2
sudo dhcpcd -h BrightSign -i $brightsg_opt60 -o $brightsn_opt55 vwlan3
sudo dhcpcd -h ZebraPrinter -i $zebra_opt60 -o $zebra_opt55 vwlan4
sudo dhcpcd -h PlaySignage -i $play_signage_opt60 -o $generic_opt55 vwlan5
sudo dhcpcd -h SONOS -i $sonos_opt60 -o $generic_opt55 vwlan6
sudo dhcpcd -h HPPrinter -i $hpprint_opt60 -o $generic_opt55 vwlan7
sudo dhcpcd -h PolyCom -i $polycom_opt60 -o $generic_opt55 vwlan8
sudo dhcpcd -h AxisCam -i $axis_opt60 -o $generic_opt55 vwlan9
sudo dhcpcd -h MoxaDevice -i $moxa_opt60 -o $moxa_opt55 vwlan11
sudo dhcpcd -h Ring -i $ring_opt60 -o $generic_opt55 vwlan12
sudo dhcpcd -h Resideo -i $resideo_opt60 -o $generic_opt55 vwlan13
sudo dhcpcd -h BarcoShare -i $barco_opt60 -o $generic_opt55 vwlan14
sudo dhcpcd -h WePresentGW -i $wepresent_opt60 -o $generic_opt55 vwlan15
sudo dhcpcd -h Desnity -i $density_opt60 -o $generic_opt55 vwlan16
sudo dhcpcd -h Oculus -i $oculus_opt60 -o $generic_opt55 vwlan17
sudo dhcpcd -h Tesla -i $tesla_opt60 -o $generic_opt55 vwlan18
sudo dhcpcd -h Crestron -i $crestron_opt60 -o $generic_opt55 vwlan19
#--------------------------------------------------------------------------------------------------------
#Updating Scripts and restarting simulation
#--------------------------------------------------------------------------------------------------------
pkill -f firefox
bash /usr/scripts/agent.sh >/dev/null 2>&1 || true
bash /usr/scripts/update_script.sh
