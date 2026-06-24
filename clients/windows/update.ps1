$version = '.03'
$logPath = 'C:\Scripts\sim.log'
$debugPath = 'C:\Scripts\debug-update.log'
$scriptRoot = 'C:\Scripts'

function Write-UpdateLog {
    param([string]$Message)
    $Message | Tee-Object -FilePath $debugPath -Append | Tee-Object -FilePath $logPath -Append | Out-Null
}

"Update Script Version $version" | Tee-Object -FilePath $debugPath
"Update Script Version $version" | Tee-Object -FilePath $logPath -Append | Out-Null
Get-Date | Tee-Object -FilePath $debugPath -Append | Tee-Object -FilePath $logPath -Append | Out-Null

Stop-Process -Name firefox -ErrorAction SilentlyContinue

if (-not (Test-Path -LiteralPath $scriptRoot)) {
    New-Item -ItemType Directory -Path $scriptRoot -Force | Out-Null
}

. 'C:\Scripts\ini-parser.ps1'
$global:iniConfig = Parse-IniFile 'C:\Scripts\simulation.conf'

$public_repo = get_value 'simulation' 'public_repo'
$repo_location = get_value 'simulation' 'repo_location'
$repo_branch = get_value 'simulation' 'repo_branch'
$smb_location = get_value 'address' 'smb_address'

Write-UpdateLog 'Updating Scripts'
$originalLocation = Get-Location

try {
    if ($public_repo -eq 'on') {
        Write-UpdateLog 'Using remote GitHub repo'
        $repoDir = Join-Path $env:USERPROFILE 'client-sim'

        Set-Location $env:USERPROFILE
        if ((Test-Path -LiteralPath $repoDir) -and -not (Test-Path -LiteralPath (Join-Path $repoDir '.git'))) {
            Write-UpdateLog 'Directory exists but is not a git repo. Removing directory.'
            Remove-Item -LiteralPath $repoDir -Recurse -Force
        }

        if (-not (Test-Path -LiteralPath $repoDir)) {
            Write-UpdateLog 'Cloning repository...'
            git clone $repo_location $repoDir 2>&1 | Tee-Object -FilePath $debugPath -Append | Out-Null
            if ($LASTEXITCODE -ne 0) {
                throw "Clone failed for $repo_location"
            }
        }

        Set-Location $repoDir
        $insideRepoOutput = git rev-parse --is-inside-work-tree 2>$null
        $insideRepo = [string]$insideRepoOutput
        if ($insideRepo) {
            $insideRepo = $insideRepo.Trim()
        }
        if ($LASTEXITCODE -ne 0 -or $insideRepo -ne 'true') {
            Write-UpdateLog 'Repo appears corrupted. Re-cloning...'
            Set-Location $env:USERPROFILE
            Remove-Item -LiteralPath $repoDir -Recurse -Force -ErrorAction SilentlyContinue
            git clone $repo_location $repoDir 2>&1 | Tee-Object -FilePath $debugPath -Append | Out-Null
            if ($LASTEXITCODE -ne 0) {
                throw "Re-clone failed for $repo_location"
            }
            Set-Location $repoDir
        }

        $currentRemoteOutput = git remote get-url origin 2>$null
        $currentRemote = [string]$currentRemoteOutput
        if ($currentRemote) {
            $currentRemote = $currentRemote.Trim()
        }
        if ($LASTEXITCODE -ne 0 -or $currentRemote -ne $repo_location) {
            Write-UpdateLog 'Remote URL mismatch. Fixing...'
            git remote remove origin 2>$null | Out-Null
            git remote add origin $repo_location 2>&1 | Tee-Object -FilePath $debugPath -Append | Out-Null
        }

        git config http.connectTimeout 5
        git config http.lowSpeedLimit 100
        git config http.lowSpeedTime 30
        git config http.maxRequests 2
        git config pull.rebase true

        git fetch origin 2>&1 | Tee-Object -FilePath $debugPath -Append | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw 'git fetch origin failed'
        }

        git show-ref --verify --quiet "refs/heads/$repo_branch"
        if ($LASTEXITCODE -eq 0) {
            Write-UpdateLog "Switching to branch: $repo_branch"
            git switch $repo_branch 2>&1 | Tee-Object -FilePath $debugPath -Append | Out-Null
        } else {
            git ls-remote --exit-code --heads origin $repo_branch 2>&1 | Tee-Object -FilePath $debugPath -Append | Out-Null
            if ($LASTEXITCODE -eq 0) {
                Write-UpdateLog "Creating branch: $repo_branch"
                git switch -c $repo_branch "origin/$repo_branch" 2>&1 | Tee-Object -FilePath $debugPath -Append | Out-Null
            } else {
                throw "Branch '$repo_branch' not found"
            }
        }

        git reset --hard "origin/$repo_branch" 2>&1 | Tee-Object -FilePath $debugPath -Append | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "git reset failed for origin/$repo_branch"
        }

        $windowsDir = Join-Path $repoDir 'windows'
        if (-not (Test-Path -LiteralPath $windowsDir)) {
            throw 'windows directory not found in repository'
        }

        Copy-Item (Join-Path $windowsDir '*.ps1') $scriptRoot -Force -ErrorAction Stop
        Copy-Item (Join-Path $windowsDir '*.txt') $scriptRoot -Force -ErrorAction SilentlyContinue

        $configsDir = Join-Path $repoDir 'configs'
        if (Test-Path -LiteralPath (Join-Path $configsDir 'simulation.conf')) {
            Copy-Item (Join-Path $configsDir 'simulation.conf') (Join-Path $scriptRoot 'simulation.conf') -Force -ErrorAction Stop
        } else {
            throw 'simulation.conf not found in configs directory'
        }
    } else {
        Write-UpdateLog 'Using local SMB repository'
        if ([string]::IsNullOrWhiteSpace($smb_location)) {
            throw 'SMB location is not defined'
        }

        if ($smb_location -like '//*') {
            $smb_location = '\\' + $smb_location.TrimStart('/').Replace('/', '\')
        }

        New-PSDrive -Name SimSMB -PSProvider FileSystem -Root $smb_location -ErrorAction Stop | Out-Null
        try {
            Copy-Item 'SimSMB:\Scripts\*' $scriptRoot -Recurse -Force -ErrorAction Stop
            if (Test-Path -LiteralPath 'SimSMB:\configs\simulation.conf') {
                Copy-Item 'SimSMB:\configs\simulation.conf' (Join-Path $scriptRoot 'simulation.conf') -Force -ErrorAction Stop
            }
        } finally {
            Remove-PSDrive -Name SimSMB -ErrorAction SilentlyContinue
        }
    }

    Write-UpdateLog 'Update complete'
} catch {
    Write-UpdateLog "Update failed: $($_.Exception.Message)"
} finally {
    Set-Location $originalLocation
}

#------------------------------------------------------------
# Web server sync (3rd source — mirrors GitHub, preferred over direct pull)
# The web server syncs all scripts and simulation.conf from GitHub.
# If web_server=on and the server is reachable, pull everything from it locally.
# Falls back silently to whatever was pulled from GitHub/SMB above.
#------------------------------------------------------------
$web_server = get_value 'simulation' 'web_server'
$server_url = get_value 'server' 'server_url'

if ($web_server -eq 'on' -and $server_url) {
    Write-UpdateLog "Checking web server: $server_url"

    $serverReachable = $false
    try {
        Invoke-WebRequest -Uri "$server_url/api/health" -UseBasicParsing `
            -TimeoutSec 10 -ErrorAction Stop | Out-Null
        $serverReachable = $true
    } catch {
        Write-UpdateLog "WARNING: Web server not reachable — keeping files from GitHub/SMB"
    }

    if ($serverReachable) {
        Write-UpdateLog 'Web server reachable — syncing files'

        # ── simulation.conf ──────────────────────────────────────────────────
        try {
            $tmpConf = Join-Path $env:TEMP 'simulation.conf.webserver'
            Invoke-WebRequest -Uri "$server_url/api/config" `
                -OutFile $tmpConf -TimeoutSec 15 -UseBasicParsing -ErrorAction Stop
            Copy-Item $tmpConf (Join-Path $scriptRoot 'simulation.conf') -Force
            Remove-Item $tmpConf -ErrorAction SilentlyContinue
            Write-UpdateLog 'simulation.conf synced from web server'
        } catch {
            Write-UpdateLog "WARNING: Failed to fetch simulation.conf from web server ($_)"
        }

        # ── Windows scripts (.ps1, .txt) ─────────────────────────────────────
        try {
            $listResponse = Invoke-WebRequest -Uri "$server_url/api/scripts/list?platform=windows" `
                -UseBasicParsing -TimeoutSec 10 -ErrorAction Stop
            $fileList = $listResponse.Content | ConvertFrom-Json

            Write-UpdateLog 'Syncing windows scripts from web server...'
            foreach ($filename in $fileList) {
                try {
                    $destPath = Join-Path $scriptRoot $filename
                    Invoke-WebRequest -Uri "$server_url/api/scripts/windows/$filename" `
                        -OutFile $destPath -TimeoutSec 30 -UseBasicParsing -ErrorAction Stop
                    Write-UpdateLog "  + $filename"
                } catch {
                    Write-UpdateLog "  ! WARNING: Failed to fetch $filename ($_)"
                }
            }
            Write-UpdateLog 'Windows script sync complete'
        } catch {
            Write-UpdateLog "WARNING: Could not get script list from web server ($_)"
        }
    }
} else {
    Write-UpdateLog 'Web server sync disabled or not configured — skipping'
}
