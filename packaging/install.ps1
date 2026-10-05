<#
.SYNOPSIS
Install claude-multi on Windows, inside a WSL 2 Linux distribution.

.DESCRIPTION
claude-multi runs on Windows through WSL 2. This script checks that WSL 2 is
available (and, when you agree, installs it), picks or creates the Linux
distribution, and runs the Linux installer (install.sh) inside it. Then it
starts `claude-multi setup` in that distribution.

claude-multi uses its own verified copy of the pinned Linux Claude Code,
which setup copies or downloads inside the distribution. A Claude Code
installed on Windows is not used and is not changed.

.PARAMETER Distribution
The WSL distribution to install into. Default: the WSL default distribution.

.PARAMETER Version
The claude-multi release to install. Default: this script's release.

.PARAMETER InstallerUrl
The https URL of install.sh. Default: the copy published with the release.

.PARAMETER InstallerSha256
The trusted sha256 of install.sh. The embedded value applies only to the
exact embedded installer URL. For any other URL, supply the checksum from
an authenticated release; verification cannot be skipped.

.PARAMETER Yes
Answer yes to every question (installing WSL 2 or a distribution).

.PARAMETER NoSetup
Do not start `claude-multi setup` after the installation.

.EXAMPLE
powershell -ExecutionPolicy Bypass -File install.ps1

.NOTES
Exit status 0: installed. 1: failed (the message says why). 3: WSL 2 or a
distribution was just installed; restart Windows or finish creating the
Linux user as the message says, then run the script again.
#>
[CmdletBinding()]
param(
    [string]$Distribution = '',
    [string]$Version = '',
    [string]$InstallerUrl = '',
    [string]$InstallerSha256 = '',
    [switch]$Yes,
    [switch]$NoSetup
)

Set-StrictMode -Version 3.0
$ErrorActionPreference = 'Stop'

function Get-ReleaseInfo {
    # The release build fills these in for the copy published with each
    # release; a source-tree copy needs -Version, -InstallerUrl and
    # a trusted -InstallerSha256.
    @{
        Version         = ''
        InstallerUrl    = ''
        InstallerSha256 = ''
    }
}

function Write-Step {
    param([string]$Message)
    Write-Host "claude-multi: $Message"
}

function Test-WindowsHost {
    [Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT
}

function Get-WindowsBuild {
    [Environment]::OSVersion.Version.Build
}

function Test-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Test-WslCommand {
    $null -ne (Get-Command 'wsl.exe' -ErrorAction SilentlyContinue)
}

function Invoke-Wsl {
    # Runs wsl.exe and returns its exit code and output. WSL_UTF8 makes
    # wsl.exe print UTF-8 instead of UTF-16; stray NULs are dropped anyway.
    param([string[]]$Arguments)
    $previous = $env:WSL_UTF8
    $env:WSL_UTF8 = '1'
    try {
        $output = (& wsl.exe @Arguments 2>&1 | Out-String)
        $code = $LASTEXITCODE
    }
    finally {
        $env:WSL_UTF8 = $previous
    }
    [pscustomobject]@{ ExitCode = $code; Output = ($output -replace "`0", '') }
}

function Invoke-WslInteractive {
    # Runs wsl.exe attached to this console (prompts reach the user).
    param([string[]]$Arguments)
    & wsl.exe @Arguments
}

function Confirm-Step {
    param([string]$Question, [switch]$AssumeYes)
    if ($AssumeYes) {
        return $true
    }
    $answer = Read-Host "claude-multi: $Question [y/N]"
    return ($answer -match '^(y|yes)$')
}

function Test-WslReady {
    if (-not (Test-WslCommand)) {
        return $false
    }
    $status = Invoke-Wsl -Arguments @('--status')
    return ($status.ExitCode -eq 0)
}

function Get-WslDistribution {
    # Parses `wsl --list --verbose`: one row per distribution, `*` marks the default.
    $listing = Invoke-Wsl -Arguments @('--list', '--verbose')
    $rows = @()
    if ($listing.ExitCode -ne 0) {
        return $rows
    }
    foreach ($line in ($listing.Output -split "\r?\n")) {
        if ($line -match '^\s*(\*)?\s*(\S+)\s+(\S+)\s+([0-9]+)\s*$') {
            $rows += [pscustomobject]@{
                Name      = $Matches[2]
                IsDefault = ($Matches[1] -eq '*')
                State     = $Matches[3]
                Version   = [int]$Matches[4]
            }
        }
    }
    return $rows
}

function Install-Wsl {
    param([switch]$AssumeYes)
    Write-Step 'WSL 2 is not installed. claude-multi runs inside a WSL 2 Linux distribution.'
    if (-not (Confirm-Step -Question 'Install WSL 2 now? It needs administrator rights and a restart.' -AssumeYes:$AssumeYes)) {
        throw 'WSL 2 is required. Install it with: wsl --install'
    }
    if (-not (Test-Administrator)) {
        throw 'Installing WSL 2 needs administrator rights. Open PowerShell as Administrator and run this script again (or run: wsl --install).'
    }
    $result = Invoke-Wsl -Arguments @('--install', '--no-distribution')
    if ($result.ExitCode -ne 0) {
        throw "wsl --install failed: $($result.Output.Trim())"
    }
    Write-Step 'WSL 2 is installed. Restart Windows, then run this script again.'
}

function New-WslDistribution {
    param([string]$Name)
    $result = Invoke-Wsl -Arguments @('--install', '--distribution', $Name)
    if ($result.ExitCode -ne 0) {
        throw "wsl --install --distribution $Name failed: $($result.Output.Trim())"
    }
    Write-Step "Finish setting up $Name (create your Linux user when it asks), then run this script again."
}

function Select-WslDistribution {
    # Returns the distribution to use, or $null after starting the creation
    # of one (the user finishes it and runs the script again).
    param([string]$Name, [switch]$AssumeYes)
    $all = @(Get-WslDistribution)
    if ($Name) {
        $found = @($all | Where-Object { $_.Name -eq $Name })
        if ($found.Count -gt 0) {
            return $found[0]
        }
        if (-not (Confirm-Step -Question "There is no WSL distribution named $Name. Install it now?" -AssumeYes:$AssumeYes)) {
            throw "No WSL distribution named $Name. List the available ones with: wsl --list --online"
        }
        New-WslDistribution -Name $Name
        return $null
    }
    $default = @($all | Where-Object { $_.IsDefault })
    if ($default.Count -gt 0) {
        return $default[0]
    }
    if ($all.Count -gt 0) {
        return $all[0]
    }
    if (-not (Confirm-Step -Question 'No WSL distribution is installed. Install Ubuntu now?' -AssumeYes:$AssumeYes)) {
        throw 'claude-multi needs a WSL 2 distribution. Install one with: wsl --install --distribution Ubuntu'
    }
    New-WslDistribution -Name 'Ubuntu'
    return $null
}

function Get-InstallerSource {
    # The install.sh URL and checksum for the requested version.
    param([string]$Version, [string]$InstallerUrl, [string]$InstallerSha256)
    $release = Get-ReleaseInfo
    if (-not $Version) {
        $Version = $release.Version
    }
    if (-not $InstallerUrl) {
        if (-not $release.InstallerUrl) {
            throw 'This copy of install.ps1 names no release. Pass -Version, -InstallerUrl and a trusted -InstallerSha256, or use the copy published with the release.'
        }
        $InstallerUrl = $release.InstallerUrl.Replace('{version}', $Version)
    }
    # The embedded checksum authenticates only the embedded install.sh URL,
    # not an arbitrary URL chosen with the same version parameter.
    $embeddedUrl = $release.InstallerUrl.Replace('{version}', $release.Version)
    if (-not $InstallerSha256 -and [string]::Equals($InstallerUrl, $embeddedUrl, [StringComparison]::Ordinal)) {
        $InstallerSha256 = $release.InstallerSha256
    }
    if ($InstallerUrl -notmatch '^https://[^\s''"]+$') {
        throw "The installer URL must be an https URL without spaces or quotes: $InstallerUrl"
    }
    if ($Version -and $Version -notmatch '^[0-9]+\.[0-9]+\.[0-9]+$') {
        throw "Not a release version: $Version"
    }
    if (-not $InstallerSha256) {
        throw 'A trusted install.sh checksum is required. Pass -InstallerSha256 from an authenticated release, or use the copy of install.ps1 published with that release.'
    }
    if ($InstallerSha256 -cnotmatch '^[0-9a-f]{64}$') {
        throw "Not a sha256 checksum: $InstallerSha256"
    }
    [pscustomobject]@{ Version = $Version; Url = $InstallerUrl; Sha256 = $InstallerSha256 }
}

function Get-BootstrapScript {
    # The Linux side, passed base64-encoded so no quoting crosses wsl.exe.
    @'
set -eu
url=${1:-}
sum=${2:-}
case "$sum" in *[!0-9a-f]*) sum='' ;; esac
if [ "${#sum}" -ne 64 ]; then
    echo 'claude-multi: a trusted install.sh sha256 checksum is required' >&2
    exit 1
fi
shift 2
cd "$HOME"
file=$(mktemp "${TMPDIR:-/tmp}/claude-multi-install.XXXXXX")
trap 'rm -f "$file"' EXIT
if command -v curl >/dev/null 2>&1; then
    curl --proto '=https' --tlsv1.2 -fsSL -o "$file" "$url"
elif command -v wget >/dev/null 2>&1; then
    wget --https-only -q -O "$file" "$url"
else
    echo 'claude-multi: this distribution has neither curl nor wget; install curl (for example: sudo apt install curl) and run install.ps1 again' >&2
    exit 1
fi
actual=$(sha256sum <"$file" | cut -d ' ' -f 1)
if [ "$actual" != "$sum" ]; then
    echo 'claude-multi: the downloaded install.sh does not match its published checksum' >&2
    exit 1
fi
sh "$file" --no-setup "$@" </dev/null
'@
}

function Get-BootstrapArgument {
    param([object]$Source, [string[]]$InstallerArguments)
    $text = ((Get-BootstrapScript) -replace "`r", '')
    $payload = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes($text))
    $sum = $Source.Sha256
    if (-not $sum -or $sum -cnotmatch '^[0-9a-f]{64}$') {
        throw 'A trusted install.sh sha256 checksum is required before starting WSL.'
    }
    $arguments = @('sh', '-c', 'echo $0 | base64 -d | sh -s -- $@', $payload, $Source.Url, $sum)
    if ($Source.Version) {
        $arguments += @('--version', $Source.Version)
    }
    foreach ($item in $InstallerArguments) {
        if ($item -notmatch '^[A-Za-z0-9._:/=+@-]+$') {
            throw "Unsupported installer argument: $item"
        }
        $arguments += $item
    }
    return $arguments
}

function Invoke-Main {
    param(
        [string]$Distribution = '',
        [string]$Version = '',
        [string]$InstallerUrl = '',
        [string]$InstallerSha256 = '',
        [string[]]$InstallerArguments = @(),
        [switch]$AssumeYes,
        [switch]$NoSetup
    )
    # Keep every caller uncaptured for the console; report status out of band.
    $script:MainExitCode = 1
    if (-not (Test-WindowsHost)) {
        throw 'install.ps1 is for Windows. On Linux and macOS, run install.sh.'
    }
    $build = Get-WindowsBuild
    if ($build -lt 19041) {
        throw "WSL 2 needs Windows 10 version 2004 (build 19041) or Windows 11; this is build $build."
    }
    $source = Get-InstallerSource -Version $Version -InstallerUrl $InstallerUrl -InstallerSha256 $InstallerSha256
    if (-not (Test-WslReady)) {
        Install-Wsl -AssumeYes:$AssumeYes
        $script:MainExitCode = 3
        return
    }
    $target = Select-WslDistribution -Name $Distribution -AssumeYes:$AssumeYes
    if ($null -eq $target) {
        $script:MainExitCode = 3
        return
    }
    if ($target.Version -ne 2) {
        throw "The distribution $($target.Name) uses WSL 1, which claude-multi does not support. Convert it with: wsl --set-version $($target.Name) 2 (then run this script again)."
    }
    Write-Step "installing into the WSL 2 distribution $($target.Name)"
    Write-Step 'claude-multi uses its own copy of the pinned Linux Claude Code, which setup gets inside this distribution; a Claude Code installed on Windows is not used.'
    $arguments = @('--distribution', $target.Name, '--exec') + (Get-BootstrapArgument -Source $source -InstallerArguments $InstallerArguments)
    Invoke-WslInteractive -Arguments $arguments
    $code = $LASTEXITCODE
    if ($code -ne 0) {
        throw "The Linux installer failed in $($target.Name) (exit $code). The messages above say why."
    }
    if ($NoSetup) {
        Write-Step "installed. Next: run 'wsl --distribution $($target.Name)' and then 'claude-multi setup'."
        $script:MainExitCode = 0
        return
    }
    Write-Step 'starting claude-multi setup'
    Invoke-WslInteractive -Arguments @('--distribution', $target.Name, '--cd', '~', '--exec', '.local/bin/claude-multi', 'setup')
    $code = $LASTEXITCODE
    if ($code -ne 0) {
        Write-Step "setup did not finish. Run 'wsl --distribution $($target.Name)' and then 'claude-multi setup' to continue."
    }
    $script:MainExitCode = 0
}

if ($MyInvocation.InvocationName -ne '.') {
    try {
        Invoke-Main -Distribution $Distribution -Version $Version -InstallerUrl $InstallerUrl `
            -InstallerSha256 $InstallerSha256 -AssumeYes:$Yes -NoSetup:$NoSetup
        exit $script:MainExitCode
    }
    catch {
        Write-Host "claude-multi: $($_.Exception.Message)" -ForegroundColor Red
        exit 1
    }
}
