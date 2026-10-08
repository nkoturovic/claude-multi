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

# Real bounded local-file reader and first-request schema, but no WSL calls.
$null = New-Item -ItemType Directory -Path $Scratch
$fixture = Join-Path $Scratch 'fixture.jsonl'
$get = '{"event":"request-parsed","method":"GET","path_category":"models","model":"fixture-model-1","stream":false,"reply":null,"carried":[]}'
$post = '{"event":"request-parsed","method":"POST","path_category":"chat-completions","model":"fixture-model-1","stream":true,"reply":1,"carried":[]}'
$second = $post.Replace('"reply":1', '"reply":2').Replace('"carried":[]', '"carried":[1]')
Assert-Equal (Test-DiagFirstFixtureRequest $Scratch) $false
[IO.File]::WriteAllText($fixture, "$get`n")
Assert-Equal (Test-DiagFirstFixtureRequest $Scratch) $false
[IO.File]::WriteAllText($fixture, "$second`n")
Assert-Equal (Test-DiagFirstFixtureRequest $Scratch) $false
[IO.File]::WriteAllText($fixture, $post.Substring(0, 20))
Assert-Equal (Test-DiagFirstFixtureRequest $Scratch) $false # Partial append cannot arm.
[IO.File]::WriteAllText($fixture, "$get`n$post`n")
Assert-Equal (Test-DiagFirstFixtureRequest $Scratch) $true
[IO.File]::WriteAllText($fixture, "$post`n" + $second.Substring(0, 20))
Assert-Equal (Test-DiagFirstFixtureRequest $Scratch) $false # Unvalidated tail stays pending.
[IO.File]::WriteAllText($fixture, "$post`n$second`n")
Assert-Equal (Test-DiagFirstFixtureRequest $Scratch) $true
# A split UTF-8 sequence in an incomplete tail must also stay pending.
[IO.File]::WriteAllBytes($fixture, ([Text.Encoding]::UTF8.GetBytes("$post`n" + '{"event":"') + [byte[]]@(195)))
Assert-Equal (Test-DiagFirstFixtureRequest $Scratch) $false
function Assert-FixtureRejected {
    param([string]$Data)
    [IO.File]::WriteAllText($fixture, $Data)
    $rejected = $false
    try { $null = Test-DiagFirstFixtureRequest $Scratch } catch { $rejected = $true }
    Assert-True $rejected
}
foreach ($invalid in @(
    '{broken}',
    $post.Replace('"method":"POST"', '"method":"POST","method":"POST"'),
    $post.Replace('fixture-model-1', 'other-model'),
    $post.Replace('chat-completions', 'other'),
    $post.Replace('"method":"POST"', '"method":"PUT"'),
    $post.Replace('request-parsed', 'delivered'),
    $post.Replace('"stream":true', '"stream":"true"'),
    $post.Replace('"reply":1', '"reply":1.0'),
    $post.Replace('"reply":1', '"reply":true'),
    $post.Replace('"reply":1', '"reply":-1'),
    $post.Replace('"carried":[]', '"carried":[1,1]'),
    $post.Replace('"carried":[]', '"carried":[2,1]'),
    $post.Replace('"carried":[]', '"carried":[true]'),
    ($post.TrimEnd('}') + ',"raw":"private"}')
)) {
    Assert-FixtureRejected "$invalid`n"
    Assert-FixtureRejected "$post`n$invalid`n" # No early return past an invalid record.
}
Assert-FixtureRejected (($get + "`n") * 257)
Assert-FixtureRejected ('x' * 4097)
Assert-FixtureRejected ('x' * 1048577)
[IO.File]::WriteAllText((Join-Path $Scratch 'namespace.json'), ('x' * 2049))
$rejected = $false
try { $null = Read-DiagJson (Join-Path $Scratch 'namespace.json') } catch { $rejected = $true }
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
function Test-DiagFirstFixtureRequest {
    param($Directory)
    $script:fixtureReads++
    if ($script:invalidFixture) { throw 'synthetic invalid fixture metadata' }
    if ($script:partialFixture) { $script:partialFixture = $false; return $false }
    return $script:hasFirstRequest
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
    $script:fixtureReads = 0
    $script:hasFirstRequest = $false
    $script:invalidFixture = $false
    $script:partialFixture = $false
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
$script:hasFirstRequest = $true
Assert-Equal (Invoke-DiagWatchdog $Scratch '/mnt/d/a/meta') 1
Assert-Equal $script:writes[0].status 'resume-timeout'
Assert-Equal $script:writes[0].armed_at_ms 179000
Assert-Equal $script:fixtureReads 1 # Repeated/rewritten fixture records cannot rearm.
Assert-Equal $script:writes[0].arm_basis 'first-fixture-request'

Reset-Scenario @(1000, 2000, 131999, 132000)
$script:hasFirstRequest = $true
$script:partialFixture = $true
Assert-Equal (Invoke-DiagWatchdog $Scratch '/mnt/d/a/meta') 1
Assert-Equal $script:writes[0].status 'resume-timeout'
Assert-Equal $script:writes[0].armed_at_ms 2000 # Only the complete validated record arms.
Assert-Equal $script:writes[0].arm_basis 'first-fixture-request'
Assert-Equal $script:fixtureReads 2

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
$script:invalidFixture = $true
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
