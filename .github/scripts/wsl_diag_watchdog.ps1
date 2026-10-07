# Windows owns the clock. Never query WSL synchronously, including on timeout.
[CmdletBinding()]
param([string]$MetadataDirectory, [string]$LinuxMetadata)
Set-StrictMode -Version 3.0
$ErrorActionPreference = 'Stop'

function Assert-DiagMetadataPath {
    param([string]$Path)
    if (-not $IsWindows -or $Path -notmatch '^[A-Za-z]:\\' -or
        ([IO.DriveInfo]::new([IO.Path]::GetPathRoot($Path))).DriveType -ne 'Fixed') {
        throw 'Metadata must be on a Windows local fixed drive, never a WSL/UNC path'
    }
    $directory = [IO.DirectoryInfo]::new($Path)
    while ($null -ne $directory) {
        if (-not $directory.Exists -or ($directory.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw 'Metadata path must contain only ordinary local directories'
        }
        $directory = $directory.Parent
    }
}

function Get-DiagDeadline {
    param([long]$ElapsedMs, [long]$ArmedAtMs)
    if ($ElapsedMs -ge 360000) { return 'absolute-timeout' }
    if ($ArmedAtMs -ge 0 -and $ElapsedMs - $ArmedAtMs -ge 130000) { return 'resume-timeout' }
    if ($ArmedAtMs -lt 0 -and $ElapsedMs -ge 180000) { return 'bootstrap-timeout' }
    return ''
}

function Get-DiagMilliseconds {
    param([object]$Clock)
    return $Clock.ElapsedMilliseconds
}

function Start-DiagProcess {
    param([string[]]$Arguments)
    $info = [Diagnostics.ProcessStartInfo]::new()
    $info.FileName = 'wsl.exe'
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    # Linux redirects all journey output to Linux-only files. Launcher errors
    # stay in bounded OS pipes, unread: no raw text reaches logs or artifacts.
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    foreach ($argument in $Arguments) { $info.ArgumentList.Add($argument) }
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $info
    try { $null = $process.Start() } catch { $process.Dispose(); throw }
    return $process
}

function Read-DiagJson {
    param([string]$Path)
    $file = [IO.FileInfo]::new($Path)
    if (-not $file.Exists) { return $null }
    if ($file.Length -gt 2048 -or ($file.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'Invalid metadata'
    }
    $stream = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, ([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete))
    try {
        $buffer = [byte[]]::new(2049)
        $count = 0
        while ($count -lt $buffer.Length) {
            $read = $stream.Read($buffer, $count, $buffer.Length - $count)
            if ($read -eq 0) { break }
            $count += $read
        }
        if ($count -gt 2048) { throw 'Invalid metadata' }
        return ([Text.UTF8Encoding]::new($false, $true).GetString($buffer, 0, $count) | ConvertFrom-Json -AsHashtable)
    } finally { $stream.Dispose() }
}

function Test-DiagResumeStart {
    param([string]$Directory)
    $row = Read-DiagJson (Join-Path $Directory 'resume-start.json')
    if ($null -eq $row) { return $false }
    $names = ($row.Keys | Sort-Object) -join ','
    if ($names -cne 'deadline_seconds,pgid,pid,schema,start_ticks' -or
        $row.schema -ne 1 -or $row.deadline_seconds -ne 120) { throw 'Invalid metadata' }
    foreach ($name in @('schema', 'deadline_seconds', 'pid', 'pgid', 'start_ticks')) {
        if ($row[$name] -isnot [long] -and $row[$name] -isnot [int]) { throw 'Invalid metadata' }
        if ($row[$name] -lt 0) { throw 'Invalid metadata' }
    }
    return $true
}

function Test-DiagNamespaceExit {
    param([string]$Directory)
    $row = Read-DiagJson (Join-Path $Directory 'namespace.json')
    $contained = Read-DiagJson (Join-Path $Directory 'containment.json')
    # An exited Windows launcher alone proves nothing about Linux children.
    return ($null -ne $row -and $row.exited -eq $true -and $row.exit_code -eq 0 -and
        $null -ne $contained -and $contained.pid_private -eq $true -and $contained.proc_private -eq $true)
}

function Write-DiagResult {
    param([string]$Directory, [hashtable]$Row)
    $path = Join-Path $Directory 'watchdog.json'
    $pending = "$path.pending"
    [IO.File]::WriteAllText($pending, (($Row | ConvertTo-Json -Depth 4 -Compress) + "`n"), [Text.UTF8Encoding]::new($false))
    [IO.File]::Move($pending, $path, $true)
}

function Stop-DiagDistribution {
    $terminate = $null
    try {
        $terminate = Start-DiagProcess -Arguments @('--terminate', 'Ubuntu-24.04')
        $clock = [Diagnostics.Stopwatch]::StartNew()
        while (-not $terminate.HasExited -and (Get-DiagMilliseconds $clock) -lt 15000) {
            Start-Sleep -Milliseconds 100
        }
        if ($terminate.HasExited) {
            if ($terminate.ExitCode -eq 0) { return 'returned' }
            return 'failed'
        }
        $terminate.Kill() # Only the Windows launcher; Linux exit remains unverified.
        return 'timeout'
    } catch { return 'launch-failed' }
    finally { if ($null -ne $terminate) { $terminate.Dispose() } }
}

function Invoke-DiagWatchdog {
    param([string]$Directory, [string]$LinuxDirectory)
    Assert-DiagMetadataPath $Directory
    if ($LinuxDirectory -notmatch '^/mnt/[a-z]/[A-Za-z0-9/_. -]+$') { throw 'Invalid Linux metadata mount path' }
    $clock = [Diagnostics.Stopwatch]::StartNew()
    $process = $null
    $armedAt = -1L
    $row = @{ schema = 1; status = 'launch-failed'; elapsed_ms = 0L; armed_at_ms = $null
        launcher_exit_code = $null; namespace_exit_confirmed = $false; terminate_state = 'pending' }
    try {
        $process = Start-DiagProcess -Arguments @('--distribution', 'Ubuntu-24.04', '--user', 'root', '--exec',
            'sh', '/home/journey/diag/wsl_diag.sh', 'run', $LinuxDirectory)
        $row.status = 'watchdog-error'
        while ($true) {
            $elapsed = Get-DiagMilliseconds $clock
            $deadline = Get-DiagDeadline $elapsed $armedAt
            if ($deadline) { $row.status = $deadline; break }
            # The first observation arms exactly once. File rewrites cannot reset it.
            if ($armedAt -lt 0) {
                try {
                    if (Test-DiagResumeStart $Directory) { $armedAt = $elapsed; $row.armed_at_ms = $armedAt }
                } catch { $row.status = 'invalid-metadata'; break }
            }
            if ($process.HasExited) {
                $row.launcher_exit_code = $process.ExitCode
                $row.namespace_exit_confirmed = Test-DiagNamespaceExit $Directory
                $row.status = 'completed'
                break
            }
            Start-Sleep -Milliseconds 250
        }
    } catch {
        # Keep only the fixed category, never an exception or WSL output.
        if ($row.status -ne 'launch-failed') { $row.status = 'watchdog-error' }
    }
    $row.elapsed_ms = Get-DiagMilliseconds $clock
    $passed = $row.status -eq 'completed' -and $row.launcher_exit_code -eq 0 -and $row.namespace_exit_confirmed
    if ($passed) { $row.terminate_state = 'not-needed' }
    # Persist the failure BEFORE even launching a possibly hung terminate command.
    Write-DiagResult $Directory $row
    if (-not $passed) {
        $row.terminate_state = Stop-DiagDistribution
        if ($null -ne $process) {
            try { if (-not $process.HasExited) { $process.Kill() } } catch { }
        }
        Write-DiagResult $Directory $row
    }
    if ($null -ne $process) { $process.Dispose() }
    if ($passed) { return 0 }
    return 1
}

if ($MyInvocation.InvocationName -ne '.') {
    exit (Invoke-DiagWatchdog -Directory $MetadataDirectory -LinuxDirectory $LinuxMetadata)
}
