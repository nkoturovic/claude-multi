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
    param([bool]$Exited = $false, [int]$Code = 0, [string]$Output = '')
    $p = [pscustomobject]@{ HasExited = $Exited; ExitCode = $Code; Killed = $false; Disposed = $false
        StandardOutput = [IO.StringReader]::new($Output) }
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
function Assert-DiagHostedWorker { }
function Assert-DiagHostedMarker { param($Directory) if ($script:markerMissing) { throw 'synthetic missing marker' } }
function Assert-DiagHostEnvironment { if ($script:hostUnsafe) { throw 'synthetic unsafe host' } }
function Start-Sleep { param($Milliseconds) }
function Get-DiagMilliseconds {
    param($Clock)
    $value = $script:times[[Math]::Min($script:index, $script:times.Count - 1)]
    $script:index++
    return $value
}
function Start-DiagProcess {
    param([string[]]$Arguments)
    $script:launches++
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
function Write-DiagJson { param($Directory, $Name, $Row) $script:jsonWrites.Add(@{ name = $Name; row = $Row.Clone() }) }
function Write-DiagResult { param($Directory, $Row) $script:writes.Add($Row.Clone()); $script:order.Add('write') }
function Request-DiagEndCapture { param($Directory) $script:order.Add('capture'); return $script:captureAvailable }
function Stop-DiagDistribution {
    # Persist success/failure and request capture BEFORE any termination.
    Assert-True ($script:writes.Count -eq 1)
    Assert-Equal $script:writes[0].terminate_state 'pending'
    Assert-Equal ($script:order -join ',') 'write,capture'
    $script:order.Add('terminate')
    $script:terminated = $true
    return $script:terminationState
}
function Reset-Scenario {
    param([long[]]$Times)
    $script:times = $Times
    $script:index = 0
    $script:fixtureReads = 0
    $script:hasFirstRequest = $false
    $script:invalidFixture = $false
    $script:partialFixture = $false
    $script:captureAvailable = $true
    $script:markerMissing = $false
    $script:hostUnsafe = $false
    $script:terminated = $false
    $script:terminationState = 'returned'
    $script:launchFails = $false
    $script:launches = 0
    $script:writes = [Collections.Generic.List[object]]::new()
    $script:jsonWrites = [Collections.Generic.List[object]]::new()
    $script:order = [Collections.Generic.List[string]]::new()
    $script:process = New-FakeProcess
}

Reset-Scenario @(179999, 180000)
Assert-Equal (Invoke-DiagWatchdog $Scratch '/mnt/d/a/meta' '/mnt/d/a/repo/candidate') 1
Assert-Equal $script:writes[0].status 'bootstrap-timeout'
Assert-True $script:terminated
Assert-True $script:process.Killed
Assert-Equal $script:writes[1].cleanup_confirmed $true
Assert-Equal $script:writes[1].mode 'hosted-disposable-wsl-fixture-only'
Assert-Equal $script:argumentsSeen[0] '--distribution'
Assert-Equal $script:argumentsSeen[-1] '/mnt/d/a/repo/candidate'
Assert-Equal ($script:argumentsSeen -contains '--user') $false
Assert-Equal ($script:argumentsSeen -contains '--cd') $true

Reset-Scenario @(179000, 308999, 309000)
$script:hasFirstRequest = $true
Assert-Equal (Invoke-DiagWatchdog $Scratch '/mnt/d/a/meta' '/mnt/d/a/repo/candidate') 1
Assert-Equal $script:writes[0].status 'resume-timeout'
Assert-Equal $script:writes[0].armed_at_ms 179000
Assert-Equal $script:fixtureReads 1 # Repeated/rewritten fixture records cannot rearm.
Assert-Equal $script:writes[0].arm_basis 'first-fixture-request'

Reset-Scenario @(1000, 2000, 131999, 132000)
$script:hasFirstRequest = $true
$script:partialFixture = $true
Assert-Equal (Invoke-DiagWatchdog $Scratch '/mnt/d/a/meta' '/mnt/d/a/repo/candidate') 1
Assert-Equal $script:writes[0].status 'resume-timeout'
Assert-Equal $script:writes[0].armed_at_ms 2000 # Only the complete validated record arms.
Assert-Equal $script:writes[0].arm_basis 'first-fixture-request'
Assert-Equal $script:fixtureReads 2

Reset-Scenario @(179999, 360000)
Assert-Equal (Invoke-DiagWatchdog $Scratch '/mnt/d/a/meta' '/mnt/d/a/repo/candidate') 1
Assert-Equal $script:writes[0].status 'absolute-timeout'

Reset-Scenario @(0)
$script:process = New-FakeProcess -Exited $true
Assert-Equal (Invoke-DiagWatchdog $Scratch '/mnt/d/a/meta' '/mnt/d/a/repo/candidate') 0
Assert-Equal $script:writes[0].cleanup_confirmed $false # Launcher exit alone is not cleanup proof.
Assert-Equal $script:writes[1].cleanup_confirmed $true
Assert-Equal $script:writes[1].capture_confirmed $true
Assert-True $script:terminated # SUCCESS also terminates the disposable distro.
Assert-Equal $script:writes.Count 2

Reset-Scenario @(0)
$script:process = New-FakeProcess -Exited $true
$script:terminationState = 'timeout'
Assert-Equal (Invoke-DiagWatchdog $Scratch '/mnt/d/a/meta' '/mnt/d/a/repo/candidate') 1
Assert-Equal $script:writes[1].cleanup_confirmed $false

Reset-Scenario @(0)
$script:process = New-FakeProcess -Exited $true -Code 1
Assert-Equal (Invoke-DiagWatchdog $Scratch '/mnt/d/a/meta' '/mnt/d/a/repo/candidate') 1
Assert-True $script:terminated # FAILURE also terminates the disposable distro.

Reset-Scenario @(0)
$script:process = New-FakeProcess -Exited $true
$script:captureAvailable = $false
Assert-Equal (Invoke-DiagWatchdog $Scratch '/mnt/d/a/meta' '/mnt/d/a/repo/candidate') 1
Assert-Equal $script:writes[1].capture_confirmed $false
Assert-True $script:terminated

Reset-Scenario @(0)
$script:invalidFixture = $true
Assert-Equal (Invoke-DiagWatchdog $Scratch '/mnt/d/a/meta' '/mnt/d/a/repo/candidate') 1
Assert-Equal $script:writes[0].status 'invalid-metadata'

Reset-Scenario @(0)
$script:launchFails = $true
Assert-Equal (Invoke-DiagWatchdog $Scratch '/mnt/d/a/meta' '/mnt/d/a/repo/candidate') 1
Assert-Equal $script:writes[0].status 'launch-failed'
Assert-True $script:terminated

Reset-Scenario @(0)
$script:hostUnsafe = $true
Assert-Equal (Invoke-DiagWatchdog $Scratch '/mnt/d/a/meta' '/mnt/d/a/repo/candidate') 1
Assert-Equal $script:launches 0 # Unsafe host stops before any journey/client launch.
Assert-True $script:terminated

Reset-Scenario @(0)
$script:markerMissing = $true
Assert-Equal (Invoke-DiagWatchdog $Scratch '/mnt/d/a/meta' '/mnt/d/a/repo/candidate') 1
Assert-Equal $script:launches 0
Assert-Equal $script:terminated $false # Never terminate an unowned/operator distribution.
Assert-Equal $script:writes[1].terminate_state 'not-owned'

Reset-Scenario @(0)
$script:process = New-FakeProcess -Exited $true
Initialize-DiagHostedWorker $Scratch
Assert-Equal $script:jsonWrites[0].name 'hosted-ci.json'
Assert-Equal $script:jsonWrites[0].row.distribution_was_absent $true
Reset-Scenario @(0)
$script:process = New-FakeProcess -Exited $true -Output 'Ubuntu-24.04'
$rejected = $false
try { Initialize-DiagHostedWorker $Scratch } catch { $rejected = $true }
Assert-True $rejected
Assert-Equal $script:jsonWrites.Count 0

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
Write-Output 'watchdog hosted mock assertions: PASS (no WSL execution)'
