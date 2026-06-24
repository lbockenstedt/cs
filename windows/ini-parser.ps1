function Remove-InlineComment {
    param([string]$Value)

    if ([string]::IsNullOrEmpty($Value)) {
        return $Value
    }

    $inSingleQuote = $false
    $inDoubleQuote = $false
    $stopParsing = $false
    $builder = New-Object System.Text.StringBuilder

    foreach ($character in $Value.ToCharArray()) {
        switch ($character) {
            "'" {
                if (-not $inDoubleQuote) {
                    $inSingleQuote = -not $inSingleQuote
                }
                [void]$builder.Append($character)
            }
            '"' {
                if (-not $inSingleQuote) {
                    $inDoubleQuote = -not $inDoubleQuote
                }
                [void]$builder.Append($character)
            }
            ';' {
                if (-not $inSingleQuote -and -not $inDoubleQuote) {
                    $stopParsing = $true
                } else {
                    [void]$builder.Append($character)
                }
            }
            '#' {
                if (-not $inSingleQuote -and -not $inDoubleQuote) {
                    $stopParsing = $true
                } else {
                    [void]$builder.Append($character)
                }
            }
            default {
                [void]$builder.Append($character)
            }
        }

        if ($stopParsing) {
            break
        }
    }

    $cleanValue = $builder.ToString().Trim()
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
            $value = Remove-InlineComment -Value $matches[2]
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
