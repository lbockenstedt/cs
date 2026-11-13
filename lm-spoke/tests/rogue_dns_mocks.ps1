# Network-free harness dot-sourced by test_rogue_dns.py after the real helpers.
# An absent keyed CIM object throws just as NetTCPIP does; broad enumeration
# returns an empty set, allowing idempotent recovery after any completed step.
$script:scenario = $env:SCENARIO
$script:manual = $script:scenario -in @('stale','no_ip','no_route','neither','restore_fail','dns_restore_fail','corrupt_restore','corrupt_operator','incomplete_restore')
# corrupt_restore/incomplete_restore still resolve through rogue_dns_server (its
# fingerprint); corrupt_operator is an operator's own static config.
$script:dns = '192.0.2.1'
if ($script:scenario -in @('corrupt_restore','incomplete_restore')) { $script:dns = '192.0.2.53' }
$script:hasIp = $script:scenario -notin @('no_ip','neither','no_lease')
$script:hasRoute = $script:scenario -notin @('no_route','neither')
$script:origin = 'Dhcp'
if ($script:manual) { $script:origin = 'Manual' }
function Add-TestCall {
    param($Text)
    Add-Content -LiteralPath (Join-Path $env:TEST_ROOT 'calls') -Value $Text
}
function Get-WlanAdapter { 'Wi-Fi' }
function Get-NetAdapter {
    [CmdletBinding()] param($Name)
    [pscustomobject]@{ InterfaceGuid = 'test-guid'; ifIndex = 7 }
}
function Get-NetIPAddress {
    [CmdletBinding()] param($InterfaceIndex, $AddressFamily)
    if (-not $script:hasIp) {
        if ($PSBoundParameters.ContainsKey('InterfaceIndex')) { throw 'No matching MSFT_NetIPAddress objects' }
        return
    }
    [pscustomobject]@{ InterfaceIndex = 7; AddressFamily = 'IPv4'; IPAddress = '192.0.2.10'; PrefixLength = 24; PrefixOrigin = $script:origin }
}
function Get-NetRoute {
    [CmdletBinding()] param($InterfaceIndex, $DestinationPrefix)
    if (-not $script:hasRoute) {
        if ($PSBoundParameters.ContainsKey('InterfaceIndex')) { throw 'No matching MSFT_NetRoute objects' }
        return
    }
    $protocol = 'Dhcp'
    if ($script:manual) { $protocol = 'NetMgmt' }
    [pscustomobject]@{ InterfaceIndex = 7; DestinationPrefix = '0.0.0.0/0'; NextHop = '192.0.2.1'; Protocol = $protocol; RouteMetric = 1 }
}
function Get-DnsClientServerAddress {
    [CmdletBinding()] param($InterfaceIndex, $AddressFamily)
    [pscustomobject]@{ ServerAddresses = @($script:dns) }
}
function Get-NetIPInterface {
    [CmdletBinding()] param($InterfaceIndex, $AddressFamily)
    $dhcp = 'Enabled'
    if ($script:manual) { $dhcp = 'Disabled' }
    [pscustomobject]@{ InterfaceIndex = 7; Dhcp = $dhcp }
}
function Remove-NetIPAddress {
    [CmdletBinding(SupportsShouldProcess)] param([Parameter(ValueFromPipeline)]$InputObject)
    process {
        if ($null -ne $InputObject) {
            if ($script:scenario -eq 'restore_fail') { throw 'Removal failed' }
            Add-TestCall 'remove ip'
            $script:hasIp = $false
        }
    }
}
function Remove-NetRoute {
    [CmdletBinding(SupportsShouldProcess)] param([Parameter(ValueFromPipeline)]$InputObject)
    process {
        if ($null -ne $InputObject) {
            Add-TestCall 'remove route'
            $script:hasRoute = $false
        }
    }
}
function Set-NetIPInterface {
    [CmdletBinding()] param($InterfaceIndex, $AddressFamily, $Dhcp)
    Add-TestCall "dhcp $Dhcp"
    if ($Dhcp -eq 'Disabled' -and $script:scenario -eq 'disabled_no_ip') {
        $script:hasIp = $false
        $script:hasRoute = $false
    }
}
function New-NetIPAddress {
    [CmdletBinding()] param($InterfaceIndex, $IPAddress, $PrefixLength, $DefaultGateway)
    $saved = Get-Content -LiteralPath (Join-Path $env:TEST_ROOT 'rogue_dns.state') -Raw | ConvertFrom-Json
    if ($saved.phase -ne 'static' -or $IPAddress -ne '192.0.2.10' -or $PrefixLength -ne 24) { throw 'State must precede apply' }
    Add-TestCall 'new ip'
    if ($script:scenario -eq 'apply_fail') { throw 'Apply failed after DHCP disabled and address removed' }
    $script:hasIp = $true
    $script:hasRoute = $true
    $script:manual = $true
    $script:origin = 'Manual'
}
function Set-DnsClientServerAddress {
    [CmdletBinding()] param($InterfaceIndex, $ServerAddresses, [switch]$ResetServerAddresses)
    if ($ResetServerAddresses) {
        if ($script:scenario -eq 'dns_restore_fail') { throw 'DNS reset failed' }
        Add-TestCall 'dns reset'
    } else {
        Add-TestCall "dns $ServerAddresses"
        # The orchestrator cancels mid-static by creating the stop file.
        if ($script:scenario -eq 'cancel') { Set-Content -LiteralPath (Join-Path $env:TEST_ROOT 'rogue_dns.stop') -Value 'stop' }
    }
}
# Tests never start real Firefox, even if installed on the runner.
function Get-Command {
    param($Name, $ErrorAction)
    return $null
}
