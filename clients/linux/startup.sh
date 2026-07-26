#!/bin/bash
version=0.01
echo ------------------------------| tee /usr/local/scripts/sim.log
echo Startup Script Version $version | tee -a /usr/local/scripts/sim.log
echo $(date) | tee -a /usr/local/scripts/sim.log
echo ------------------------------| tee -a /usr/local/scripts/sim.log
#------------------------------------------------------------
# Put /usr/local/scripts on PATH so 'bash update.sh' / 'bash dns_fail.sh
# --verbose' work from any login shell without typing the full path. This
# startup terminal is busy running the sim loop, so an export here alone
# wouldn't reach a separate SSH/manual shell — also drop a profile.d fragment
# (sourced by every login shell) via best-effort sudo -n (no TTY hang).
# install.sh deploys the same fragment at install time; this catches already-
# deployed clients. Idempotent: skip when it already contains the path.
#------------------------------------------------------------
case ":$PATH:" in *:/usr/local/scripts:*) ;; *) export PATH="$PATH:/usr/local/scripts" ;; esac
_profile_d=/etc/profile.d/client-sim-path.sh
if ! grep -qs '/usr/local/scripts' "$_profile_d" 2>/dev/null; then
  if printf '%s\n' '# Managed by client-sim — do not edit manually' \
                    '# Put the sim scripts on PATH for every login shell.' \
                    'case ":$PATH:" in' \
                    '  *:/usr/local/scripts:*) ;;' \
                    '  *) export PATH="$PATH:/usr/local/scripts" ;;' \
                    'esac' | sudo -n tee "$_profile_d" >/dev/null 2>&1; then
    sudo -n chmod 644 "$_profile_d" 2>/dev/null || true
    echo "Added /usr/local/scripts to PATH (profile.d self-heal)" | tee -a /usr/local/scripts/sim.log
  else
    echo "PATH profile.d self-heal skipped (no sudo — install.sh backstop)" | tee -a /usr/local/scripts/sim.log
  fi
fi
#------------------------------------------------------------
#Check Logs Script
#------------------------------------------------------------
bash /usr/local/scripts/sys_mon.sh --log-monitor &
#------------------------------------------------------------
#Verify key settings changed - since this script is ran at startup
#this is where you should put system changes you want to make sure 
#are applied. Some of these may be set during the installer but the 
#installer is only ran one time.
#------------------------------------------------------------
echo Disabling screen blanking | tee -a /usr/local/scripts/sim.log
gsettings set org.gnome.desktop.session idle-delay 0
# Cut compositor (mutter) CPU on these GPU-less VMs: disable desktop animations
# so the software (llvmpipe) compositor isn't repainting transitions. Pairs with
# the 120s dashboard interval to reduce steady mutter CPU across the fleet.
gsettings set org.gnome.desktop.interface enable-animations false 2>/dev/null || true
xset s noblank
xset -dpms
xset s off
sudo rfkill unblock wifi; sudo rfkill unblock all
#------------------------------------------------------------
#Calling config parser script
#------------------------------------------------------------
echo Reading Simulation Config File | tee -a /usr/local/scripts/sim.log
#------------------------------------------------------------
#Calling config parser script - reads the simulation.conf file
#For values assinged to script variables
#------------------------------------------------------------
source '/usr/local/scripts/ini-parser.sh'
# Shared helpers (derive_username/derive_bucket/adapter detection) — canonical
# source clients/lib/common.sh.
source '/usr/local/scripts/common.sh'
#------------------------------------------------------------
# Print a full table of every deployed script's version + the CI-maintained
# deploy VERSION, so the boot terminal shows FOR SURE what code is on this box.
#------------------------------------------------------------
sim_versions_report
#------------------------------------------------------------
#Figuring out username from hostname used to parse config
#------------------------------------------------------------
derive_username
#------------------------------------------------------------
#Setting config file location
#------------------------------------------------------------
process_ini_file '/usr/local/scripts/simulation.conf'
#------------------------------------------------------------
echo ------------------------------| tee -a /usr/local/scripts/sim.log
echo Parsing Config File | tee -a /usr/local/scripts/sim.log
#------------------------------------------------------------
#Settings read from the local config file
#Global Simulation settings
#------------------------------------------------------------
# cached once per boot — see common.sh derive_bucket
derive_bucket
simulation_id="s${bucket}"
user_sim_id=$(get_value "$username" 'simulation_id')
# Only accept valid slot IDs (s0-s9); old scripts used character-position hashing
# which could produce letters (e.g. "su"). Reject and fall back to hash bucket.
if [[ -n "$user_sim_id" && ! "$user_sim_id" =~ ^s[0-9]$ ]]; then
  echo "WARNING: invalid simulation_id '${user_sim_id}' for ${HOSTNAME} — old hashing method detected, using hash bucket ${simulation_id}" | tee -a /usr/local/scripts/sim.log
elif [[ "$user_sim_id" =~ ^s[0-9]$ ]]; then
  simulation_id="$user_sim_id"
fi
reboot_schedule=$(get_value 'simulation' 'reboot_schedule')
repo_location=$(get_value 'simulation' 'repo_location')
sim_phy=$(get_value $simulation_id 'sim_phy')
rapid_update=$(get_value 'simulation' 'rapid_update')
syslog=$(get_value 'simulation' 'syslog')
syslog_server=$(get_value 'address' 'syslog_server')
#------------------------------------------------------------
# Cap the GNOME compositor CPU — the biggest idle CPU sink on these GPU-less VM
# desktops (software / llvmpipe compositing). On modern GNOME the compositor
# process is `gnome-shell` (mutter is a library linked INTO gnome-shell, NOT a
# separate process), so `cpulimit -e mutter` matched nothing and the cap never
# attached — the compositor ran uncapped at ~40% CPU. Target gnome-shell by name,
# with mutter as a fallback for older GNOME where it IS a separate process. This
# script runs INSIDE the GNOME session (gsettings works above), so the compositor
# is already up and cpulimit finds it immediately. cpulimit is installed by
# apt_update.sh. Configurable via simulation.conf [simulation] mutter_cpu_limit =
# percent of ONE core; unset falls back to 5 (aggressive — these are non-
# interactive sim desktops, so hard-cap the compositor). Set 0 to disable.
# Idempotent: drop any prior cap before re-applying on each boot.
#------------------------------------------------------------
mutter_cpu_limit=$(get_value 'simulation' 'mutter_cpu_limit')
[[ "$mutter_cpu_limit" =~ ^[0-9]+$ ]] || mutter_cpu_limit=5
if (( mutter_cpu_limit > 0 )); then
  if ! command -v cpulimit >/dev/null 2>&1; then
    echo "WARNING: cpulimit not installed — cannot cap compositor CPU (apt_update.sh installs it)" | tee -a /usr/local/scripts/sim.log
  else
    pkill -f 'cpulimit -e mutter' 2>/dev/null || true
    pkill -f 'cpulimit -e gnome-shell' 2>/dev/null || true
    capped=""
    if pgrep -x gnome-shell >/dev/null 2>&1; then
      cpulimit -e gnome-shell -l "$mutter_cpu_limit" -b >/dev/null 2>&1 || true
      capped="gnome-shell"
    elif pgrep -x mutter >/dev/null 2>&1; then
      cpulimit -e mutter -l "$mutter_cpu_limit" -b >/dev/null 2>&1 || true
      capped="mutter"
    else
      echo "WARNING: no GNOME compositor (gnome-shell/mutter) running yet — compositor CPU not capped" | tee -a /usr/local/scripts/sim.log
    fi
    [ -n "$capped" ] && echo "Capping ${capped} compositor CPU at ${mutter_cpu_limit}% of one core (cpulimit -e)" | tee -a /usr/local/scripts/sim.log
  fi
fi
tempvar=$(get_value $username 'repo_location')
#------------------------------------------------------------
#Checking to see if this device/user has an override
#------------------------------------------------------------
if [[ -n ${tempvar} ]]; then repo_location=$tempvar; fi
tempvar=$(get_value $username 'sim_phy')
if [[ -n ${tempvar} ]]; then sim_phy=$tempvar; fi
#------------------------------------------------------------
#Configuring Syslog Server
#------------------------------------------------------------
if [[ "$syslog" == "on" ]]; then
  #Ensure the remote syslog line exists, replace if different
  if grep -q '^\*\.\*@' /etc/rsyslog.conf; then
    sudo sed -i "s|^\*\.\*@.*|*.*@${syslog_server}|" /etc/rsyslog.conf
  else
    # Insert before imuxsock if no syslog line exists
    sudo sed -i "/module(load=\"imuxsock\")/i *.*@${syslog_server}" /etc/rsyslog.conf
  fi
  #Add the comment line before the syslog line, if it doesn't exist
  if ! grep -Fxq "#Syslog Server" /etc/rsyslog.conf; then
    sudo sed -i "/\*\.\*@${syslog_server}/i #Syslog Server" /etc/rsyslog.conf
  fi
  #Ensure the imfile module exists before imuxsock
  if ! grep -Eq '^\s*(\$ModLoad\s+imfile|module\(load="imfile"\))' /etc/rsyslog.conf; then
    sudo sed -i '/module(load="imuxsock")/i $ModLoad imfile' /etc/rsyslog.conf
  fi
else
 echo Skipping Syslog Server Update | tee -a /usr/local/scripts/sim.log
fi
#------------------------------------------------------------
#Scheduling Reboot
#------------------------------------------------------------
# reboot_schedule is in MINUTES. Guard: if it's missing/non-numeric/<=0, skip
# scheduling (an empty value used to eval as 0 → shutdown -r +0 = immediate
# reboot). shutdown -r +N schedules N minutes from now (a bare N is seconds).
if [[ "$reboot_schedule" =~ ^[0-9]+$ ]] && (( reboot_schedule > 0 )); then
  rn=$(( reboot_schedule + RANDOM % 600 ))
  echo Scheduling reboot $rn minutes | tee -a /usr/local/scripts/sim.log
  shutdown -r +$rn
else
  echo "Skipping reboot schedule (reboot_schedule missing/invalid: '${reboot_schedule}')" | tee -a /usr/local/scripts/sim.log
fi
#Making sure eth0 and wlan0 are online
echo Bringing up all interfaces online | tee -a /usr/local/scripts/sim.log
#------------------------------------------------------------
#Finding adapter names and setting usable variables for interfaces
#When using a physical piece of hardware we want to diable the
#interface not in use. So that we force the traffic out the interface
#set int he simulation.conf
#------------------------------------------------------------
#------------------------------------------------------------
#Finding adapter names and setting usable variables for interfaces
#------------------------------------------------------------
detect_wlan_adapter
detect_eth_adapter
if [[ -n ${wladapter} ]]; then echo WLAN Adapter name $wladapter | tee -a /usr/local/scripts/sim.log; fi
if [[ -n ${eadapter} ]]; then echo Wired Adapter name $eadapter | tee -a /usr/local/scripts/sim.log; fi
if [[ -n ${wladapter} ]]; then sudo ip link set dev $wladapter up; fi
if [[ -n ${eadapter} ]]; then sudo ip link set dev $eadapter up; fi
echo -----------------------------| tee -a /usr/local/scripts/sim.log
#------------------------------------------------------------
#Running Updates
#------------------------------------------------------------
echo Updating Simulation from repo | tee -a /usr/local/scripts/sim.log
source '/usr/local/scripts/update.sh'
#------------------------------------------------------------
echo Setting Script Permissions | tee -a /usr/local/scripts/sim.log
echo -----------------------------| tee -a /usr/local/scripts/sim.log
cd /usr/local/scripts/ && sudo chmod +x *.sh &
#------------------------------------------------------------
#Looping Script
#------------------------------------------------------------
echo Launching Simulation Script | tee -a /usr/local/scripts/sim.log
source /usr/local/scripts/simulation.sh
