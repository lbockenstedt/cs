# Trim a value and unwrap matching outer quotes. There are no inline comments: the
# spoke writes simulation.conf with Python's default configparser, which reads ;
# and # inside a value as part of it, so "ab #12" must stay whole (as in the
# Linux parser). Only whole-line comments are skipped (Parse-IniFile).
function ConvertFrom-IniValue {
    param([string]$Value)

    if ([string]::IsNullOrEmpty($Value)) {
        return $Value
    }

    $cleanValue = $Value.Trim()
    if ($cleanValue.Length -ge 2) {
        if (($cleanValue.StartsWith('"') -and $cleanValue.EndsWith('"')) -or ($cleanValue.StartsWith("'") -and $cleanValue.EndsWith("'"))) {
            $cleanValue = $cleanValue.Substring(1, $cleanValue.Length - 2)
        }
    }

    return $cleanValue.Trim()
}

function Parse-IniFile {
    param([string]$filePath)

    $ini = @{}
    $section = 'default'
    $ini[$section] = @{}

    if (-not (Test-Path -LiteralPath $filePath)) {
        Write-Warning "INI file not found: $filePath"
        return $ini
    }

    foreach ($rawLine in Get-Content -LiteralPath $filePath -ErrorAction SilentlyContinue) {
        $line = $rawLine.Trim()
        if ([string]::IsNullOrWhiteSpace($line) -or $line.StartsWith('#') -or $line.StartsWith(';')) {
            continue
        }

        if ($line -match '^\[(.+?)\]\s*$') {
            $section = $matches[1].Trim()
            if (-not $ini.ContainsKey($section)) {
                $ini[$section] = @{}
            }
            continue
        }

        if ($line -match '^([^=]+?)\s*=\s*(.*)$') {
            $key = $matches[1].Trim()
            $value = ConvertFrom-IniValue -Value $matches[2]
            $ini[$section][$key] = $value
        }
    }

    return $ini
}

function get_value {
    param([string]$section, [string]$key)

    if ($null -eq $global:iniConfig) {
        return $null
    }

    if ($global:iniConfig.ContainsKey($section) -and $global:iniConfig[$section].ContainsKey($key)) {
        return $global:iniConfig[$section][$key]
    }

    return $null
}
