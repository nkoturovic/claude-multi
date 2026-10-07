# Host-only assertions and process/clock mocks. Never starts wsl.exe.
param([string]$Harness, [string]$Scratch)
$ErrorActionPreference = 'Stop'
. $Harness

function Assert-Equal {
    param($Actual, $Expected)
    if ($Actual -cne $Expected) { throw "Assertion failed: expected $Expected, got $Actual" }
}
function Assert-True {
    param($Value)
    if (-not $Value) { throw 'Assertion failed' }
}
# Parse every inline PowerShell block as data, without executing the workflow.
$github = Split-Path -Parent (Split-Path -Parent $Harness)
$lines = [IO.File]::ReadAllLines((Join-Path $github 'workflows/release.yml'))
$parsedBlocks = 0
for ($i = 0; $i -lt $lines.Count; $i++) {
    if ($lines[$i] -notmatch '^\s+run: \|$') { continue }
    $indent = [regex]::Match($lines[$i], '^ *').Length + 2
    $block = [Collections.Generic.List[string]]::new()
    for ($j = $i + 1; $j -lt $lines.Count; $j++) {
        if ($lines[$j].Trim() -and [regex]::Match($lines[$j], '^ *').Length -lt $indent) { break }
        if ($lines[$j].Length -ge $indent) { $block.Add($lines[$j].Substring($indent)) }
        else { $block.Add('') }
    }
    $parseTokens = $null
    $parseErrors = $null
    $null = [Management.Automation.Language.Parser]::ParseInput(($block -join "`n"), [ref]$parseTokens, [ref]$parseErrors)
    Assert-Equal @($parseErrors).Count 0
    $parsedBlocks++
}
Assert-True ($parsedBlocks -gt 0)

function New-FakeProcess {
    param([bool]$Exited = $false, [int]$Code = 0)
    $p = [pscustomobject]@{ HasExited = $Exited; ExitCode = $Code; Killed = $false; Disposed = $false }
    $p | Add-Member -MemberType ScriptMethod -Name Kill -Value { $this.Killed = $true }
    $p | Add-Member -MemberType ScriptMethod -Name Dispose -Value { $this.Disposed = $true }
    return $p
}

Assert-Equal (Get-DiagDeadline 179999 -1) ''
Assert-Equal (Get-DiagDeadline 180000 -1) 'bootstrap-timeout'
Assert-Equal (Get-DiagDeadline 129999 0) ''
Assert-Equal (Get-DiagDeadline 130000 0) 'resume-timeout'
Assert-Equal (Get-DiagDeadline 308999 179000) ''
Assert-Equal (Get-DiagDeadline 309000 179000) 'resume-timeout'
Assert-Equal (Get-DiagDeadline 360000 359999) 'absolute-timeout'
Assert-Equal (Get-DiagDeadline 360000 -1) 'absolute-timeout'

# Real bounded local-file reader and marker schema, but no WSL calls.
$null = New-Item -ItemType Directory -Path $Scratch
Assert-Equal (Test-DiagResumeStart $Scratch) $false
[IO.File]::WriteAllText((Join-Path $Scratch 'resume-start.json'),
    '{"schema":1,"pid":9,"pgid":9,"start_ticks":123,"deadline_seconds":120}')
Assert-Equal (Test-DiagResumeStart $Scratch) $true
[IO.File]::WriteAllText((Join-Path $Scratch 'resume-start.json'), '{"schema":1,"argv":"private"}')
$rejected = $false
try { $null = Test-DiagResumeStart $Scratch } catch { $rejected = $true }
Assert-True $rejected
[IO.File]::WriteAllText((Join-Path $Scratch 'resume-start.json'), ('x' * 2049))
$rejected = $false
try { $null = Read-DiagJson (Join-Path $Scratch 'resume-start.json') } catch { $rejected = $true }
Assert-True $rejected
$rejected = $false
try { Assert-DiagMetadataPath '\\wsl.localhost\Ubuntu-24.04\home\journey' } catch { $rejected = $true }
Assert-True $rejected

$realStop = ${function:Stop-DiagDistribution}
function Assert-DiagMetadataPath { param($Path) }
function Start-Sleep { param($Milliseconds) }
function Get-DiagMilliseconds {
    param($Clock)
    $value = $script:times[[Math]::Min($script:index, $script:times.Count - 1)]
    $script:index++
    return $value
}
function Start-DiagProcess {
    param([string[]]$Arguments)
    $script:argumentsSeen = $Arguments
    if ($script:launchFails) { throw 'synthetic launch failure' }
    return $script:process
}
function Test-DiagResumeStart {
    param($Directory)
    $script:markerReads++
    if ($script:invalidMarker) { throw 'synthetic invalid marker' }
    return $script:hasMarker
}
function Test-DiagNamespaceExit { param($Directory) return $script:namespaceExited }
function Write-DiagResult {
    param($Directory, $Row)
    $script:writes.Add($Row.Clone())
}
function Stop-DiagDistribution {
    # The first failure result MUST already be durable before termination.
    Assert-True ($script:writes.Count -eq 1)
    Assert-Equal $script:writes[0].terminate_state 'pending'
    $script:terminated = $true
    return 'returned'
}
function Reset-Scenario {
    param([long[]]$Times)
    $script:times = $Times
    $script:index = 0
    $script:markerReads = 0
    $script:hasMarker = $false
    $script:invalidMarker = $false
    $script:namespaceExited = $false
    $script:terminated = $false
    $script:launchFails = $false
    $script:writes = [Collections.Generic.List[object]]::new()
    $script:process = New-FakeProcess
}

Reset-Scenario @(179999, 180000)
Assert-Equal (Invoke-DiagWatchdog $Scratch '/mnt/d/a/meta') 1
Assert-Equal $script:writes[0].status 'bootstrap-timeout'
Assert-True $script:terminated
Assert-True $script:process.Killed
Assert-Equal $script:writes[1].namespace_exit_confirmed $false
Assert-Equal $script:argumentsSeen[0] '--distribution'
Assert-Equal $script:argumentsSeen[-1] '/mnt/d/a/meta'

Reset-Scenario @(179000, 308999, 309000)
$script:hasMarker = $true
Assert-Equal (Invoke-DiagWatchdog $Scratch '/mnt/d/a/meta') 1
Assert-Equal $script:writes[0].status 'resume-timeout'
Assert-Equal $script:writes[0].armed_at_ms 179000
Assert-Equal $script:markerReads 1 # Repeated/rewritten markers cannot rearm.

Reset-Scenario @(179999, 360000)
Assert-Equal (Invoke-DiagWatchdog $Scratch '/mnt/d/a/meta') 1
Assert-Equal $script:writes[0].status 'absolute-timeout'

Reset-Scenario @(0)
$script:process = New-FakeProcess -Exited $true
Assert-Equal (Invoke-DiagWatchdog $Scratch '/mnt/d/a/meta') 1
Assert-Equal $script:writes[0].status 'completed'
Assert-Equal $script:writes[0].namespace_exit_confirmed $false
Assert-True $script:terminated # Launcher exit alone is not Linux cleanup proof.

Reset-Scenario @(0)
$script:process = New-FakeProcess -Exited $true
$script:namespaceExited = $true
Assert-Equal (Invoke-DiagWatchdog $Scratch '/mnt/d/a/meta') 0
Assert-Equal $script:writes[0].terminate_state 'not-needed'
Assert-Equal $script:terminated $false
Assert-Equal $script:writes.Count 1

Reset-Scenario @(0)
$script:invalidMarker = $true
Assert-Equal (Invoke-DiagWatchdog $Scratch '/mnt/d/a/meta') 1
Assert-Equal $script:writes[0].status 'invalid-metadata'

Reset-Scenario @(0)
$script:launchFails = $true
Assert-Equal (Invoke-DiagWatchdog $Scratch '/mnt/d/a/meta') 1
Assert-Equal $script:writes[0].status 'launch-failed'
Assert-True $script:terminated

# Restore the actual termination loop. Its process and clocks stay mocked.
Set-Item Function:Stop-DiagDistribution $realStop
Reset-Scenario @(0, 14999, 15000)
Assert-Equal (Stop-DiagDistribution) 'timeout'
Assert-True $script:process.Killed
Assert-True $script:process.Disposed
Assert-Equal ($script:argumentsSeen -join ',') '--terminate,Ubuntu-24.04'

Reset-Scenario @(0)
$script:process = New-FakeProcess -Exited $true -Code 1
Assert-Equal (Stop-DiagDistribution) 'failed'
Reset-Scenario @(0)
$script:process = New-FakeProcess -Exited $true
Assert-Equal (Stop-DiagDistribution) 'returned'
Reset-Scenario @(0)
$script:launchFails = $true
Assert-Equal (Stop-DiagDistribution) 'launch-failed'
Write-Output 'watchdog mock assertions: PASS (no WSL execution)'
