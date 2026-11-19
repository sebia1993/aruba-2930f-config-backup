# Windows PowerShell 5.1 or later. Run -WhatIf to inspect the scope first.
[CmdletBinding(SupportsShouldProcess = $true)]
param([switch]$Remove)

$ErrorActionPreference = 'Stop'
$ruleName = 'Aruba2930F.LocalWeb.LAN'
$ruleGroup = 'Aruba 2930F Local Web'
$ruleTitle = 'Aruba 2930F Local Web - network sharing'
$listenPort = 18766

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
$settings = @{
    Name = $ruleName
    PolicyStore = 'PersistentStore'
    Direction = 'Inbound'
    Action = 'Allow'
    Enabled = 'True'
    Protocol = 'TCP'
    LocalPort = $listenPort
    LocalAddress = $listener.Address
    RemoteAddress = 'Any'
    InterfaceAlias = $nic.InterfaceAlias
    Program = $listener.Program
    Profile = @('Domain', 'Private', 'Public')
    EdgeTraversalPolicy = 'Block'
}
$scope = "$($listener.Program) on $($nic.InterfaceAlias), $($listener.Address):$listenPort from any IPv4 address"
if ($PSCmdlet.ShouldProcess($scope, 'Allow inbound HTTPS for Aruba 2930F Local Web')) {
    Assert-Administrator
    if (Get-OwnedRule) {
        Set-NetFirewallRule @settings -NewDisplayName $ruleTitle
    } else {
        New-NetFirewallRule @settings -DisplayName $ruleTitle -Group $ruleGroup | Out-Null
    }
    Write-Output "Allowed inbound HTTPS on the selected IPv4 interface. Use this script with -Remove to remove the rule."
}
