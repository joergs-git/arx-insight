<#
ARX Insight - Windows Firewall helper for the phone access (v0.6.0).

Adds ONE inbound rule (or removes it with -Remove): TCP, the app's port and the five after it, only
from the LOCAL SUBNET, only for private / domain networks. Needs administrator rights: the app starts
this script with a UAC prompt when the owner clicks "Allow in Windows Firewall" - the installer itself
never elevates, and phone access stays off until the owner switches it on.

If the Windows prompt "Allow Python to communicate on these networks?" was once answered with
"Cancel", Windows created BLOCK rules for that python.exe, and a block rule beats every allow rule.
-Program names our interpreter(s); inbound block rules for exactly these programs are DISABLED (not
deleted - they stay visible in "Windows Defender Firewall with Advanced Security").

Public domain / CC0. No warranty.
#>
param(
    [int]$Port = 8765,
    [switch]$Remove,
    [string]$Program = ""
)
$ErrorActionPreference = "Stop"
$name = "ARX Insight (phone access)"

$me = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "Administrator rights are needed to change the Windows Firewall."
    exit 1
}

# always start clean: never two rules with our name
Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue | Remove-NetFirewallRule

if ($Remove) {
    Write-Host "Removed the rule '$name'."
    exit 0
}

$last = $Port + 5
New-NetFirewallRule -DisplayName $name `
    -Description "ARX Insight: lets phones in the same local network open the app (opt-in phone access). Local subnet only." `
    -Direction Inbound -Action Allow -Protocol TCP -LocalPort "$Port-$last" `
    -RemoteAddress LocalSubnet -Profile Private, Domain | Out-Null
Write-Host "Added the rule '$name' (TCP $Port-$last, local subnet, private / domain networks)."

# a block rule for our python.exe would still win - switch exactly those off
foreach ($path in ($Program -split ";" | Where-Object { $_ })) {
    try {
        Get-NetFirewallApplicationFilter -Program $path -ErrorAction SilentlyContinue | ForEach-Object {
            $rule = $_ | Get-NetFirewallRule
            if ($rule.Direction -eq "Inbound" -and $rule.Action -eq "Block" -and $rule.Enabled -eq "True") {
                $rule | Disable-NetFirewallRule
                Write-Host "Disabled a block rule for $path ($($rule.DisplayName))."
            }
        }
    } catch {
        Write-Host "Could not check block rules for $path : $($_.Exception.Message)"
    }
}
exit 0
