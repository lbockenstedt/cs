#!/bin/bash
version=.14
pkill -f firefox
log="/usr/local/scripts/sim.log"
debug="/usr/local/scripts/debug-update.log"
# Ensure log files exist and are writable by the running user.
# /usr/local/scripts/ is root-owned; files created there by sudo are root-owned
# and unwritable by the simulation user.  Touch + chmod once here before any tee.
sudo -n touch "$log" "$debug" 2>/dev/null && sudo -n chmod a+w "$log" "$debug" 2>/dev/null || true
echo "Update Script Version $version" | tee "$debug"
echo "$(date)" | tee -a "$debug"
source '/usr/local/scripts/ini-parser.sh'
process_ini_file '/usr/local/scripts/simulation.conf'

#------------------------------------------------------------
# Read config values
#------------------------------------------------------------
web_server=$(get_value 'simulation' 'web_server')
server_url=$(get_value 'server' 'server_url')
server_url="${server_url:-http://169.253.1.1:8080}"
smb_repo=$(get_value 'simulation' 'smb_repo')
smb_address=$(get_value 'address' 'smb_address')
github_repo=$(get_value 'simulation' 'github_repo')
repo_location=$(get_value 'simulation' 'repo_location')
repo_branch=$(get_value 'simulation' 'repo_branch')

source_found=false

_return_or_exit() {
    if [[ "${BASH_SOURCE[0]}" != "${0}" ]]; then
        return "${1:-0}"
    else
        exit "${1:-0}"
    fi
}

#------------------------------------------------------------
# Compare two dotted-numeric versions ($1=remote, $2=local).
# A leading dot means an implicit 0 major (".103" → 0.103, "1.50" → 1.50).
# Echoes "newer", "equal" or "older" describing remote relative to local.
# Used to gate syncs so we only pull when remote is STRICTLY NEWER — an equal
# version is up-to-date and an older remote is never applied (no downgrade).
#------------------------------------------------------------
_version_cmp() {
    local a="$1" b="$2"
    [[ "$a" == .* ]] && a="0$a"
    [[ "$b" == .* ]] && b="0$b"
    local IFS=.
    local -a av=($a) bv=($b)
    local i max=${#av[@]}
    (( ${#bv[@]} > max )) && max=${#bv[@]}
    for (( i=0; i<max; i++ )); do
        local ai="${av[i]:-0}" bi="${bv[i]:-0}"
        ai="${ai//[^0-9]/}"; bi="${bi//[^0-9]/}"
        ai=$((10#${ai:-0})); bi=$((10#${bi:-0}))
        if (( ai > bi )); then echo "newer"; return 0; fi
        if (( ai < bi )); then echo "older"; return 0; fi
    done
    echo "equal"
    return 0
}

#------------------------------------------------------------
# Suppress duplicate nm-applet tray icon.
# linux/nm-applet.desktop (Hidden=true) is distributed to ~/.config/autostart/
# by the API/GitHub sync, overriding the system /etc/xdg/autostart/nm-applet.desktop.
# This function only needs to handle the lxsession side (@nm-applet in autostart).
#------------------------------------------------------------
suppress_nm_applet() {
    local _lxsession_sys="/etc/xdg/lxsession/LXDE-pi/autostart"
    local _lxsession_user="$HOME/.config/lxsession/LXDE-pi/autostart"
    if [ -f "$_lxsession_sys" ]; then
        mkdir -p "$(dirname "$_lxsession_user")"
        if [ ! -f "$_lxsession_user" ]; then
            grep -v 'nm-applet' "$_lxsession_sys" > "$_lxsession_user" || true
        elif grep -q 'nm-applet' "$_lxsession_user"; then
            sed -i '/nm-applet/d' "$_lxsession_user"
        fi
    fi
}

#------------------------------------------------------------
# Helper: copy files from a local directory into /usr/local/scripts
# Called after a successful web or SMB sync
#------------------------------------------------------------
copy_local_files() {
    local src_dir="$1"
    echo "Copying files from $src_dir..." | tee -a "$debug"
    shopt -s nullglob
    local sh_files=( "$src_dir"/*.sh )
    local txt_files=( "$src_dir"/*.txt )
    local desktop_files=( "$src_dir"/*.desktop )
    local conf_files=( "$src_dir"/simulation.conf )

    # /usr/local/scripts is chown root:sim-user chmod 775 — group-writable without
    # sudo. All copies to that directory use plain cp/mv/chmod to avoid sudo
    # password prompts hanging in non-interactive (no-TTY) contexts.
    # sudo is reserved only for system dirs (/etc, /var) that require root.

    # Copy all .sh files except update.sh first — update.sh is copied last so
    # that if bash re-reads this file after the copy it doesn't hit a parse error.
    # Track success: VERSION is only committed if all .sh copies succeed, so a
    # partial failure leaves local VERSION unchanged and the next cycle retries.
    local _copy_ok=true
    for _f in "${sh_files[@]}"; do
        [[ "$(basename "$_f")" == "update.sh" ]] && continue
        if ! cp --remove-destination "$_f" /usr/local/scripts/; then
            echo "ERROR: failed to copy $(basename "$_f") — aborting VERSION commit" | tee -a "$debug" "$log"
            _copy_ok=false
        fi
    done
    # Never copy kill_switch.txt — gkill_switch is always fetched live at runtime
    local filtered_txt=()
    for _t in "${txt_files[@]}"; do
        [[ "$(basename "$_t")" == "kill_switch.txt" ]] && continue
        filtered_txt+=("$_t")
    done
    (( ${#filtered_txt[@]} )) && cp --remove-destination "${filtered_txt[@]}" /usr/local/scripts/
    # Deploy .desktop files to the user autostart dir (~/.config/autostart/).
    # LXDE/Pi OS reads both /etc/xdg/autostart/ (system, root-owned) and
    # ~/.config/autostart/ (user, no sudo needed). User dir takes precedence,
    # so updates land here without requiring sudo cp to the system dir.
    _user_autostart="$HOME/.config/autostart"
    mkdir -p "$_user_autostart"
    for _d in "${desktop_files[@]}"; do
        _dname=$(basename "$_d")
        cp -f "$_d" "$_user_autostart/$_dname" \
            && echo "Updated $_dname" | tee -a "$debug" \
            || echo "WARNING: could not update $_dname" | tee -a "$debug"
    done
    suppress_nm_applet
    (( ${#conf_files[@]} )) && cp --remove-destination "${conf_files[@]}" /usr/local/scripts/

    if [[ -f "$src_dir/user-overrides.conf" ]]; then
        cp --remove-destination "$src_dir/user-overrides.conf" /usr/local/scripts/user-overrides.conf
    fi

    # System config files — these dirs are root-owned, sudo required.
    # Reload rsyslog only when the file actually changed, so the newly
    # distributed config takes effect immediately instead of waiting for the
    # next reboot (imfile tail of sim.log → syslog forwarding).
    if [[ -f "$src_dir/10-rsyslog.conf" ]]; then
        if ! cmp -s "$src_dir/10-rsyslog.conf" /etc/rsyslog.d/10-rsyslog.conf 2>/dev/null; then
            if sudo -n cp "$src_dir/10-rsyslog.conf" /etc/rsyslog.d/10-rsyslog.conf 2>/dev/null; then
                sudo -n systemctl restart rsyslog 2>/dev/null || \
                    sudo -n pkill -HUP rsyslogd 2>/dev/null || \
                    echo "WARNING: rsyslog.conf updated but reload failed (no sudo)" | tee -a "$debug"
            else
                echo "WARNING: could not update rsyslog.conf (no sudo)" | tee -a "$debug"
            fi
        fi
    fi
    # Deploy polkit rule to suppress NM graphical auth dialogs
    if [[ -f "$src_dir/50-client-sim-nm.rules" ]]; then
        sudo -n mkdir -p /etc/polkit-1/rules.d 2>/dev/null
        sudo -n cp "$src_dir/50-client-sim-nm.rules" /etc/polkit-1/rules.d/50-client-sim-nm.rules 2>/dev/null || \
            echo "WARNING: could not update polkit rules (no sudo)" | tee -a "$debug"
    fi
    # update.sh: atomic inode swap (cp to .new + mv) instead of cp-in-place.
    # cp truncates the existing file in place (same inode); bash has that inode
    # open and reads garbled content as the new bytes stream in. mv replaces
    # the directory entry atomically — bash keeps the old fd and finishes reading
    # the old content cleanly, then the next `source update.sh` gets the new file.
    # This runs BEFORE the VERSION commit and folds its result into _copy_ok so a
    # failed update.sh copy blocks the VERSION bump — otherwise the box would be
    # stuck on the old update.sh while claiming the new version.
    if [[ -f "$src_dir/update.sh" ]]; then
        if ! { cp "$src_dir/update.sh" /usr/local/scripts/update.sh.new \
                && chmod a+rx /usr/local/scripts/update.sh.new \
                && mv -f /usr/local/scripts/update.sh.new /usr/local/scripts/update.sh; }; then
            echo "ERROR: failed to copy update.sh — aborting VERSION commit" | tee -a "$debug" "$log"
            _copy_ok=false
        fi
    fi
    # Only commit VERSION if all .sh copies (including update.sh) succeeded —
    # ensures next update cycle retries the full sync rather than treating a
    # partial copy as complete.
    if [[ "$_copy_ok" == true && -f "$src_dir/VERSION" ]]; then
        cp "$src_dir/VERSION" /usr/local/scripts/VERSION.new \
            && mv -f /usr/local/scripts/VERSION.new /usr/local/scripts/VERSION \
            || echo "ERROR: VERSION commit failed" | tee -a "$debug" "$log"
    elif [[ -f "$src_dir/VERSION" ]]; then
        echo "Skipping VERSION commit — one or more script copies failed" | tee -a "$debug" "$log"
    fi
    chmod a+rx /usr/local/scripts/*.sh 2>/dev/null || true
    chmod a+rw "$log" "$debug" 2>/dev/null || true
}

#------------------------------------------------------------
# Helper: check if the web server API is genuinely up
# Step 1 - TCP port reachable (rules out ICMP-only responses and dead IPs)
# Step 2 - HTTP 200 + JSON body contains "status":"ok"
# Ping is intentionally NOT used; a pingable IP does not mean the API is up.
#------------------------------------------------------------
check_api_up() {
    local url="$1"
    # Parse host and port from URL (http://host:port[/path])
    local host port
    host=$(echo "$url" | sed -E 's|https?://([^:/]+).*|\1|')
    port=$(echo "$url" | sed -E 's|https?://[^:]+:([0-9]+).*|\1|')
    [[ -z "$port" ]] && port=80

    echo "Checking TCP $host:$port ..." | tee -a "$debug"
    if ! timeout 3 bash -c "cat < /dev/null > /dev/tcp/$host/$port" 2>/dev/null; then
        echo "TCP port $port on $host is not open — API is DOWN" | tee -a "$debug" "$log"
        return 1
    fi

    echo "TCP open. Checking HTTP response..." | tee -a "$debug"
    local tmp
    tmp=$(mktemp)
    local http_code
    http_code=$(curl -sS --max-time 5 -o "$tmp" -w "%{http_code}" "$url/api/health" 2>/dev/null)
    local body
    body=$(cat "$tmp")
    rm -f "$tmp"

    if [[ "$http_code" != "200" ]]; then
        echo "HTTP check failed (code: $http_code) — API is DOWN" | tee -a "$debug" "$log"
        return 1
    fi
    if ! echo "$body" | grep -q '"status"[[:space:]]*:[[:space:]]*"ok"'; then
        echo "HTTP 200 but body missing status:ok — API is DOWN" | tee -a "$debug" "$log"
        return 1
    fi

    echo "API confirmed UP" | tee -a "$debug"
    return 0
}

#============================================================
# Config sync from the spoke — ALWAYS, independent of the script update tier
#============================================================
# The [username] engine/quota assignments are injected by the spoke at
# /api/config serve time; they live ONLY on the spoke, never in GitHub. Tier 1
# below pulls the config when web_server=on — but a web_server=off client updates
# its SCRIPTS from GitHub (Tier 2) and would then NEVER receive its engine
# assignment. So pull the config from the spoke here whenever Tier 1 won't.
if [[ "$web_server" != "on" && -n "$server_url" ]] && check_api_up "$server_url"; then
    echo "Config sync from spoke ($server_url) — web_server off, pulling /api/config anyway" | tee -a "$debug" "$log"
    _cfg_tmp=$(mktemp)
    _cfg_code=$(curl -sS --max-time 10 -o "$_cfg_tmp" -w "%{http_code}" \
        "$server_url/api/config?hostname=$(hostname)" 2>/dev/null)
    if [[ "$_cfg_code" == "200" && -s "$_cfg_tmp" ]]; then
        if ! diff -q "$_cfg_tmp" /usr/local/scripts/simulation.conf >/dev/null 2>&1; then
            echo "simulation.conf changed (spoke) — updating" | tee -a "$debug" "$log"
            mv -f -- "$_cfg_tmp" /usr/local/scripts/simulation.conf
        else
            rm -f "$_cfg_tmp"
        fi
    else
        echo "Config fetch failed (code: $_cfg_code) — keeping existing" | tee -a "$debug"
        rm -f "$_cfg_tmp"
    fi
    # Human per-user pins (user-overrides.conf) — 404 is acceptable.
    _ov_tmp=$(mktemp)
    _ov_code=$(curl -sS --max-time 10 -o "$_ov_tmp" -w "%{http_code}" \
        "$server_url/api/config/overrides" 2>/dev/null)
    if [[ "$_ov_code" == "200" && -s "$_ov_tmp" ]] && ! diff -q "$_ov_tmp" /usr/local/scripts/user-overrides.conf >/dev/null 2>&1; then
        mv -f -- "$_ov_tmp" /usr/local/scripts/user-overrides.conf
    else
        rm -f "$_ov_tmp"
    fi
fi

#============================================================
# TIER 1 — Web Server
#============================================================
echo "Updating Scripts" | tee -a "$debug" "$log"

if [[ "$web_server" == "on" && -n "$server_url" ]]; then
    echo "Tier 1: Trying Web Server ($server_url)..." | tee -a "$debug"

    if check_api_up "$server_url"; then
        # Version check — only do full sync if remote VERSION differs from local
        local_ver=$(cat /usr/local/scripts/VERSION 2>/dev/null | tr -d '[:space:]')
        remote_ver=$(curl -sS --max-time 5 \
            "$server_url/api/scripts/linux/VERSION" 2>/dev/null | tr -d '[:space:]')
        echo "Version check: local=$local_ver remote=$remote_ver" | tee -a "$debug"
        _vcmp=""
        [[ -n "$remote_ver" ]] && _vcmp=$(_version_cmp "$remote_ver" "$local_ver")
        if [[ -z "$remote_ver" ]]; then
            echo "remote VERSION empty — skipping sync" | tee -a "$debug" "$log"
        elif [[ "$_vcmp" == "older" ]]; then
            echo "remote older ($remote_ver < $local_ver) — skipping (no downgrade)" | tee -a "$debug" "$log"
        elif [[ "$_vcmp" == "equal" ]]; then
            echo "Already up to date (v$local_ver) — checking for config changes..." | tee -a "$debug" "$log"
            # Scripts are current, but simulation.conf may have changed independently.
            # Always fetch and apply it so kill_switch / other config tweaks propagate
            # without requiring a full script version bump.
            _cfg_tmp=$(mktemp)
            _cfg_code=$(curl -sS --max-time 10 \
                -o "$_cfg_tmp" \
                -w "%{http_code}" \
                "$server_url/api/config?hostname=$(hostname)" 2>/dev/null)
            if [[ "$_cfg_code" == "200" && -s "$_cfg_tmp" ]]; then
                if ! diff -q "$_cfg_tmp" /usr/local/scripts/simulation.conf >/dev/null 2>&1; then
                    echo "simulation.conf changed — updating" | tee -a "$debug" "$log"
                    # Atomic write: mv is atomic on the same filesystem, prevents
                    # simulation.sh reading a partial file during rapid_update cycles
                    mv -f -- "$_cfg_tmp" /usr/local/scripts/simulation.conf
                else
                    echo "simulation.conf unchanged" | tee -a "$debug"
                fi
            else
                echo "Config fetch failed (code: $_cfg_code) — keeping existing" | tee -a "$debug"
            fi
            rm -f "$_cfg_tmp"

            # Also always fetch user-overrides.conf — it can change without a version bump.
            _ov_tmp=$(mktemp)
            _ov_code=$(curl -sS --max-time 10 \
                -o "$_ov_tmp" \
                -w "%{http_code}" \
                "$server_url/api/config/overrides" 2>/dev/null)
            if [[ "$_ov_code" == "200" && -s "$_ov_tmp" ]]; then
                if ! diff -q "$_ov_tmp" /usr/local/scripts/user-overrides.conf >/dev/null 2>&1; then
                    echo "user-overrides.conf changed — updating" | tee -a "$debug" "$log"
                    mv -f -- "$_ov_tmp" /usr/local/scripts/user-overrides.conf
                else
                    echo "user-overrides.conf unchanged" | tee -a "$debug"
                fi
            else
                echo "user-overrides.conf not available (code: $_ov_code) — keeping existing" | tee -a "$debug"
                rm -f "$_ov_tmp"
            fi

            source_found=true
        else
            echo "Update available ($local_ver → $remote_ver) — syncing..." | tee -a "$debug" "$log"
            tmp_web=$(mktemp -d)
            sync_ok=true

            # Pull simulation.conf with hostname-specific overrides
            http_code=$(curl -sS --max-time 10 \
                -o "$tmp_web/simulation.conf" \
                -w "%{http_code}" \
                "$server_url/api/config?hostname=$(hostname)" 2>/dev/null)
            if [[ "$http_code" != "200" || ! -s "$tmp_web/simulation.conf" ]]; then
                echo "Config download failed (code: $http_code)" | tee -a "$debug" "$log"
                sync_ok=false
            fi

            # Pull user-overrides.conf (404 is acceptable — file may not exist in repo)
            if [[ "$sync_ok" == true ]]; then
                ov_code=$(curl -sS --max-time 10 \
                    -o "$tmp_web/user-overrides.conf" \
                    -w "%{http_code}" \
                    "$server_url/api/config/overrides" 2>/dev/null)
                if [[ "$ov_code" != "200" ]]; then
                    echo "user-overrides.conf not available (code: $ov_code) — skipping" | tee -a "$debug"
                    rm -f "$tmp_web/user-overrides.conf"
                fi
            fi

            # Pull script list and download each file
            if [[ "$sync_ok" == true ]]; then
                script_list=$(curl -sS --max-time 10 \
                    "$server_url/api/scripts/list?platform=linux" 2>/dev/null)
                if [[ -z "$script_list" ]]; then
                    echo "Script list empty or unreachable — falling through" | tee -a "$debug" "$log"
                    sync_ok=false
                else
                    for fname in $(echo "$script_list" | tr -d '[]"' | tr ',' '\n' | tr -d ' '); do
                        [[ -z "$fname" ]] && continue
                        fcode=$(curl -sS --max-time 15 \
                            -o "$tmp_web/$fname" \
                            -w "%{http_code}" \
                            "$server_url/api/scripts/linux/$fname" 2>/dev/null)
                        if [[ "$fcode" != "200" ]]; then
                            echo "Failed to download $fname (code: $fcode)" | tee -a "$debug" "$log"
                            sync_ok=false
                            break
                        fi
                    done
                fi
            fi

            if [[ "$sync_ok" == true ]]; then
                echo "Web server sync succeeded" | tee -a "$debug" "$log"
                copy_local_files "$tmp_web"
                # Persist a copy of API-downloaded files as a local fallback cache.
                # Prevents an older GitHub version from clobbering API-customised
                # scripts and configs (e.g. per-device simulation.conf) when the
                # API is temporarily unavailable on the next boot.
                API_CACHE="/usr/local/scripts/.api-cache"
                mkdir -p "$API_CACHE"
                cp -r "$tmp_web"/. "$API_CACHE/"
                # Cached scripts get executed later — never leave them
                # world-writable. Dir 755; .sh readable+executable, others
                # readable; strip group/other write from every cached file.
                chmod 755 "$API_CACHE"
                find "$API_CACHE" -type f -name "*.sh" -exec chmod a+rx {} +
                find "$API_CACHE" -type f ! -name "*.sh" -exec chmod a+r {} +
                find "$API_CACHE" -type f -exec chmod go-w {} +
                echo "API cache updated at $API_CACHE" | tee -a "$debug"
                # Also copy API files into the local git repo clone so that the
                # GitHub fallback tier finds the latest version already in place
                # and doesn't overwrite per-device configs with template versions.
                _repo_linux=~/client-sim/linux
                _repo_configs=~/client-sim/configs
                if [[ -d "$_repo_linux" ]]; then
                    cp "$tmp_web"/*.sh  "$_repo_linux/" 2>/dev/null || true
                    cp "$tmp_web"/*.txt "$_repo_linux/" 2>/dev/null || true
                    [[ -f "$tmp_web/VERSION" ]] && cp "$tmp_web/VERSION" "$_repo_linux/VERSION"
                    echo "Local git repo linux/ updated from API" | tee -a "$debug"
                fi
                if [[ -d "$_repo_configs" && -f "$tmp_web/simulation.conf" ]]; then
                    cp "$tmp_web/simulation.conf" "$_repo_configs/simulation.conf"
                    echo "Local git repo configs/ updated from API" | tee -a "$debug"
                fi
                source_found=true
            else
                echo "Web server reachable but sync incomplete — falling through" | tee -a "$debug" "$log"
            fi
            rm -rf "$tmp_web"
        fi
    else
        echo "Web server unreachable — skipping Tier 1" | tee -a "$debug" "$log"
    fi
fi

#============================================================
# TIER 2 — Local API Cache (last successful API download)
# Used when API is unavailable. Prevents falling back to a
# potentially older GitHub/SMB version when the API was the
# source of truth for scripts and per-device configs.
#============================================================
API_CACHE="/usr/local/scripts/.api-cache"
if [[ "$source_found" == false && "$web_server" == "on" && -f "$API_CACHE/VERSION" ]]; then
    echo "Tier 2: Trying local API cache ($API_CACHE)..." | tee -a "$debug"
    cache_ver=$(cat "$API_CACHE/VERSION" 2>/dev/null | tr -d '[:space:]')
    local_ver=$(cat /usr/local/scripts/VERSION 2>/dev/null | tr -d '[:space:]')
    echo "Cache version: $cache_ver  Installed: $local_ver" | tee -a "$debug"
    if [[ -n "$cache_ver" && "$cache_ver" == "$local_ver" ]]; then
        echo "Already running cached version (v$cache_ver) — no update needed" | tee -a "$debug" "$log"
        source_found=true
    elif [[ -n "$cache_ver" ]]; then
        echo "Applying cached API files (v$local_ver → v$cache_ver)..." | tee -a "$debug" "$log"
        copy_local_files "$API_CACHE"
        source_found=true
    fi
fi

#============================================================
# TIER 3 — GitHub
#============================================================
if [[ "$source_found" == false && "$github_repo" == "on" ]]; then
    echo "Tier 3: Trying GitHub ($repo_location)..." | tee -a "$debug"
    cd ~ || { echo "WARNING: Failed to cd to home directory" | tee -a "$debug"; _return_or_exit 1; }
    repo_dir="client-sim"
    shopt -s nullglob

    if [[ -d "$repo_dir" && ! -d "$repo_dir/.git" ]]; then
        echo "Directory exists but is not a git repo. Removing..." | tee -a "$debug"
        rm -rf "$repo_dir"
    fi
    if [[ ! -d "$repo_dir" ]]; then
        echo "Cloning repository..." | tee -a "$debug"
        git clone "$repo_location" "$repo_dir" || { echo "ERROR: Clone failed" | tee -a "$debug" "$log"; }
    fi

    if cd "$repo_dir" 2>/dev/null; then
        current_remote=$(git remote get-url origin 2>/dev/null || echo "")
        if [[ "$current_remote" != "$repo_location" ]]; then
            echo "Remote URL mismatch. Fixing..." | tee -a "$debug" "$log"
            git remote set-url origin "$repo_location"
        fi

        if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
            echo "Repo corrupted. Re-cloning..." | tee -a "$debug" "$log"
            cd ~
            rm -rf "$repo_dir"
            git clone "$repo_location" "$repo_dir"
            cd "$repo_dir" || { echo "ERROR: Cannot enter repo" | tee -a "$debug" "$log"; }
        fi

        git config --global http.connectTimeout 5
        git config http.lowSpeedLimit 100
        git config http.lowSpeedTime 30
        git config http.maxRequests 2
        git config pull.rebase true
        git fetch origin

        if git show-ref --verify --quiet "refs/heads/$repo_branch"; then
            git switch "$repo_branch"
        elif git ls-remote --exit-code --heads origin "$repo_branch" >/dev/null 2>&1; then
            git switch -c "$repo_branch" "origin/$repo_branch"
        else
            echo "ERROR: Branch '$repo_branch' not found" | tee -a "$debug" "$log"
        fi

        git reset --hard "origin/$repo_branch"

        # git reset --hard wipes any files Tier 1 seeded into this clone from the
        # API cache (latest scripts + per-device configs). Re-apply the cached
        # copies over the freshly reset repo so the GitHub fallback uses the
        # API-current versions and doesn't clobber per-device simulation.conf
        # with the repo template.
        API_CACHE="/usr/local/scripts/.api-cache"
        if [[ -d "$API_CACHE" ]]; then
            echo "Re-seeding repo from API cache after reset..." | tee -a "$debug"
            [[ -f "$API_CACHE/simulation.conf" && -d configs ]] && \
                cp "$API_CACHE/simulation.conf" configs/simulation.conf 2>/dev/null || true
            if [[ -d linux ]]; then
                cp "$API_CACHE"/*.sh  linux/ 2>/dev/null || true
                cp "$API_CACHE"/*.txt linux/ 2>/dev/null || true
                [[ -f "$API_CACHE/VERSION" ]] && cp "$API_CACHE/VERSION" linux/VERSION 2>/dev/null || true
            fi
        fi

        # Version check before copying — skip if already at this version
        local_ver=$(cat /usr/local/scripts/VERSION 2>/dev/null | tr -d '[:space:]')
        remote_ver=$(cat linux/VERSION 2>/dev/null | tr -d '[:space:]')
        echo "Version check: local=$local_ver remote=$remote_ver" | tee -a "$debug"
        _vcmp=""
        [[ -n "$remote_ver" ]] && _vcmp=$(_version_cmp "$remote_ver" "$local_ver")
        if [[ -z "$remote_ver" ]]; then
            echo "remote VERSION empty — skipping sync" | tee -a "$debug" "$log"
        elif [[ "$_vcmp" == "older" ]]; then
            echo "remote older ($remote_ver < $local_ver) — skipping (no downgrade)" | tee -a "$debug" "$log"
        elif [[ "$_vcmp" == "equal" ]]; then
            echo "Already up to date (v$local_ver) — checking for config changes..." | tee -a "$debug" "$log"
            # Scripts are current but configs/ may have changed. Always apply
            # simulation.conf (and user-overrides.conf) so config tweaks like
            # kill_switch propagate without requiring a script version bump.
            [[ -f "configs/simulation.conf" ]]     && cp --remove-destination "configs/simulation.conf"     /usr/local/scripts/simulation.conf
            [[ -f "configs/user-overrides.conf" ]]  && cp --remove-destination "configs/user-overrides.conf"  /usr/local/scripts/user-overrides.conf
            source_found=true
        else
            echo "Update available ($local_ver → $remote_ver) — copying files..." | tee -a "$debug" "$log"
            if cd linux 2>/dev/null; then
                shopt -s nullglob
                desktop_files=( *.desktop )
                sh_files=( *.sh )
                txt_files=( *.txt )
                if [[ -f "10-rsyslog.conf" ]] && ! cmp -s "10-rsyslog.conf" /etc/rsyslog.d/10-rsyslog.conf 2>/dev/null; then
                    sudo -n cp 10-rsyslog.conf /etc/rsyslog.d/10-rsyslog.conf 2>/dev/null && \
                        { sudo -n systemctl restart rsyslog 2>/dev/null || sudo -n pkill -HUP rsyslogd 2>/dev/null || true; }
                fi
                # Deploy .desktop files to user autostart dir (no sudo needed)
                _user_autostart="$HOME/.config/autostart"
                mkdir -p "$_user_autostart"
                for _d in "${desktop_files[@]}"; do
                    cp -f "$_d" "$_user_autostart/$_d" \
                        && echo "Updated $_d" | tee -a "$debug" \
                        || echo "WARNING: could not deploy $_d to autostart" | tee -a "$debug"
                done
                # Copy all .sh except update.sh first; update.sh copied last
                for _f in "${sh_files[@]}"; do
                    [[ "$_f" == "update.sh" ]] && continue
                    cp --remove-destination "$_f" /usr/local/scripts/
                done
                # Never copy kill_switch.txt — gkill_switch always fetched live
                _filtered_txt=()
                for _t in "${txt_files[@]}"; do
                    [[ "$_t" == "kill_switch.txt" ]] && continue
                    _filtered_txt+=("$_t")
                done
                (( ${#_filtered_txt[@]} )) && cp --remove-destination "${_filtered_txt[@]}" /usr/local/scripts/
                [[ -f "VERSION" ]] && cp --remove-destination VERSION /usr/local/scripts/VERSION
                cd ..
            else
                echo "WARNING: linux directory not found" | tee -a "$debug"
            fi

            if cd configs 2>/dev/null; then
                [[ -f "simulation.conf" ]]     && cp --remove-destination simulation.conf     /usr/local/scripts/simulation.conf
                [[ -f "user-overrides.conf" ]] && cp --remove-destination user-overrides.conf /usr/local/scripts/user-overrides.conf
                cd ..
            else
                echo "WARNING: configs directory not found" | tee -a "$debug"
            fi

            # update.sh: atomic mv to avoid bash re-read corruption (same fix as copy_local_files)
            if [[ -f "linux/update.sh" ]]; then
                cp linux/update.sh /usr/local/scripts/update.sh.new \
                    && chmod a+rx /usr/local/scripts/update.sh.new \
                    && mv -f /usr/local/scripts/update.sh.new /usr/local/scripts/update.sh \
                    || true
            fi
            chmod a+rx /usr/local/scripts/*.sh 2>/dev/null || true
            echo "GitHub sync succeeded" | tee -a "$debug" "$log"
            source_found=true
        fi
    else
        echo "ERROR: Could not enter repo directory" | tee -a "$debug" "$log"
    fi
fi

#============================================================
# TIER 4 — SMB Share (last resort)
#============================================================
if [[ "$source_found" == false && "$smb_repo" == "on" && -n "$smb_address" ]]; then
    echo "Tier 4: Trying SMB ($smb_address)..." | tee -a "$debug"
    tmp_smb=$(mktemp -d)
    if smbclient "$smb_address" -N -c "lcd $tmp_smb; cd Scripts; prompt; mget *" 2>/dev/null; then
        echo "SMB sync succeeded" | tee -a "$debug" "$log"
        copy_local_files "$tmp_smb"
        source_found=true
    else
        echo "SMB sync failed — falling through" | tee -a "$debug" "$log"
    fi
    rm -rf "$tmp_smb"
fi

#============================================================
# Result
#============================================================
if [[ "$source_found" == false ]]; then
    echo "ERROR: All update sources failed — no files updated" | tee -a "$debug" "$log"
fi
#============================================================
# Run inbox agent — check for pending commands from server
#============================================================
if [[ "$web_server" == "on" && -n "$server_url" ]]; then
    if [[ -f /usr/local/scripts/agent.sh ]]; then
        bash /usr/local/scripts/agent.sh
    fi
fi

#============================================================
# Always run nm-applet suppression — ensures it takes effect even
# when the device was already up to date and copy_local_files() didn't run
#============================================================
suppress_nm_applet

echo "Update complete" | tee -a "$debug"
