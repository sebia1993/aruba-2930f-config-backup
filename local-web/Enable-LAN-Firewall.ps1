# Windows PowerShell 5.1 or later. Run -WhatIf to inspect the scope first.
[CmdletBinding(SupportsShouldProcess = $true)]
param([switch]$Remove)

$ErrorActionPreference = 'Stop'
$ruleName = 'Aruba2930F.LocalWeb.LAN'
$ruleGroup = 'Aruba 2930F Local Web'
$ruleTitle = 'Aruba 2930F Local Web - same subnet HTTPS'
$listenPort = 18766

function Get-ConnectedSubnet([System.Net.IPAddress]$Address, [int]$Prefix) {
    if ($Address.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork -or
        $Prefix -lt 8 -or $Prefix -gt 31) {
        throw 'Choose a connected IPv4 LAN interface in the application.'
    }
    $bytes = $Address.GetAddressBytes()
    if ($bytes[0] -eq 0 -or $bytes[0] -eq 127 -or $bytes[0] -ge 224 -or
        ($bytes[0] -eq 169 -and $bytes[1] -eq 254)) {
        throw 'Choose a connected IPv4 LAN interface in the application.'
    }
    for ($offset = 0; $offset -lt $bytes.Count; $offset++) {
        $bits = [Math]::Min(8, [Math]::Max(0, $Prefix - 8 * $offset))
        $mask = 256 - [int][Math]::Pow(2, 8 - $bits)
        $bytes[$offset] = [byte]($bytes[$offset] -band $mask)
    }
    return "$([System.Net.IPAddress]::new($bytes))/$Prefix"
}

function Assert-Administrator {
    $current = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($current)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Open PowerShell as Administrator to change the Windows firewall rule.'
    }
}

function Get-OwnedRule {
    $found = Get-NetFirewallRule -Name $ruleName -PolicyStore PersistentStore -ErrorAction SilentlyContinue
    if ($found -and $found.Group -cne $ruleGroup) {
        throw 'The firewall rule name belongs to another application. No change was made.'
    }
    return $found
}

if ($Remove) {
    if ($PSCmdlet.ShouldProcess($ruleTitle, 'Remove the application firewall rule')) {
        Assert-Administrator
        if (Get-OwnedRule) {
            Remove-NetFirewallRule -Name $ruleName -PolicyStore PersistentStore
        }
    }
    return
}

$server = (Get-Item -LiteralPath (Join-Path $PSScriptRoot 'server.py')).FullName
$runtime = Join-Path $PSScriptRoot 'runtime'
$allowedExecutables = @((Join-Path $runtime 'python.exe'), (Join-Path $runtime 'pythonw.exe'))
# The product launcher runs the adjacent, absolute server.py as the script argument.
$launchPattern = '^\s*(?:"[^"]+"|\S+)\s+(?:-B\s+)?(?:-X\s+utf8\s+)?(?:"' +
    [regex]::Escape($server) + '"|' + [regex]::Escape($server) + ')(?:\s|$)'
$matchesFound = @(
    foreach ($socket in @(Get-NetTCPConnection -State Listen -LocalPort $listenPort -ErrorAction SilentlyContinue)) {
        $owner = Get-CimInstance Win32_Process -Filter "ProcessId=$($socket.OwningProcess)"
        if ($owner -and $allowedExecutables -contains $owner.ExecutablePath -and
            $owner.CommandLine -match $launchPattern) {
            [PSCustomObject]@{ Address = $socket.LocalAddress; Program = $owner.ExecutablePath }
        }
    }
)
if ($matchesFound.Count -ne 1) {
    throw 'Enable LAN sharing in this copy of Aruba 2930F Local Web, then run this script from the same extracted folder.'
}
$listener = $matchesFound[0]
$interfaces = @(Get-NetIPAddress -AddressFamily IPv4 -IPAddress $listener.Address -AddressState Preferred)
if ($interfaces.Count -ne 1) {
    throw 'The listening address must belong to exactly one connected interface.'
}
$nic = $interfaces[0]
$subnet = Get-ConnectedSubnet $listener.Address $nic.PrefixLength
$settings = @{
    Name = $ruleName
    PolicyStore = 'PersistentStore'
    Direction = 'Inbound'
    Action = 'Allow'
    Enabled = 'True'
    Protocol = 'TCP'
    LocalPort = $listenPort
    LocalAddress = $listener.Address
    RemoteAddress = $subnet
    InterfaceAlias = $nic.InterfaceAlias
    Program = $listener.Program
    Profile = @('Domain', 'Private', 'Public')
    EdgeTraversalPolicy = 'Block'
}
$scope = "$($listener.Program) on $($nic.InterfaceAlias), $($listener.Address):$listenPort from $subnet"
if ($PSCmdlet.ShouldProcess($scope, 'Allow same-subnet inbound HTTPS for Aruba 2930F Local Web')) {
    Assert-Administrator
    if (Get-OwnedRule) {
        Set-NetFirewallRule @settings
    } else {
        New-NetFirewallRule @settings -DisplayName $ruleTitle -Group $ruleGroup | Out-Null
    }
    Write-Output "Allowed same-subnet HTTPS. Use this script with -Remove to remove the rule."
}
