# Windows owns the clock. Never query WSL synchronously, including on timeout.
[CmdletBinding()]
param([string]$MetadataDirectory, [string]$LinuxMetadata, [string]$LinuxWorkspace)
Set-StrictMode -Version 3.0
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Text.Json
$script:DiagMode = 'hosted-disposable-wsl-fixture-only'

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

function Assert-DiagHostedWorker {
    # No bypass flag: never launch/terminate an operator's local distribution.
    if (-not $IsWindows -or [Environment]::OSVersion.Version.Build -lt 26100 -or
        $env:GITHUB_ACTIONS -cne 'true' -or $env:CI -cne 'true' -or $env:RUNNER_OS -cne 'Windows' -or
        $env:RUNNER_ENVIRONMENT -cne 'github-hosted' -or -not $env:RUNNER_NAME -or
        $env:GITHUB_EVENT_NAME -cne 'workflow_dispatch' -or
        $env:GITHUB_REPOSITORY -cne 'nkoturovic/claude-multi' -or $env:GITHUB_REF -cne 'refs/heads/ci/wsl-resume-diag' -or
        $env:GITHUB_RUN_ID -notmatch '^[0-9]+$' -or $env:GITHUB_RUN_ATTEMPT -notmatch '^[0-9]+$') {
        throw 'Hosted disposable Windows worker required'
    }
}

function Assert-DiagHostEnvironment {
    # Inspect names only, never values, and never silently unset an override.
    $credential = '(?i)(?:^|_)(?:APIKEY|ACCESSKEY|ACCESSTOKEN|AUTH(?:ORIZATION)?|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIALS?|COOKIE|KEY|JWT|BEARER|PAT)(?:_|$)'
    $selection = @('CLAUDECODE', 'CLAUDE_CONFIG_DIR', 'CLAUDE_MODEL', 'CLAUDE_CODE_MODEL',
        'CLAUDE_CODE_SUBAGENT_MODEL', 'CLAUDE_CODE_SUBAGENT_MODEL_FORCE', 'CLAUDE_CODE_DISABLE_FAST_MODE',
        'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY',
        'DEPLOY', 'WRITABLE_PATH', 'MANAGEMENT_STATIC_PATH', 'META_MINT_URL')
    foreach ($name in [Environment]::GetEnvironmentVariables().Keys) {
        if ($name -match $credential -or $selection -contains $name.ToUpperInvariant() -or
            $name -match '^(?i:CLAUDE_MULTI_|ANTHROPIC_|CLAUDE_CODE_USE_|PGSTORE_|GITSTORE_|OBJECTSTORE_)') {
            throw 'Hosted fixture environment refused'
        }
    }
    $home = [Environment]::GetFolderPath([Environment+SpecialFolder]::UserProfile)
    if (-not $home) { throw 'Fresh hosted Windows home required' }
    $paths = @('.claude', '.claude.json', '.anthropic', '.config\claude', '.config\claude-multi', '.local\share\claude-multi\auth',
        'AppData\Roaming\Claude', 'AppData\Roaming\ClaudeCode')
    foreach ($relative in $paths) {
        $path = Join-Path $home $relative
        $directory = [IO.DirectoryInfo]::new($path)
        $file = [IO.FileInfo]::new($path)
        if ($directory.Exists) {
            if ($directory.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Fresh hosted Windows home required' }
            $entries = $directory.EnumerateFileSystemInfos().GetEnumerator()
            try { if ($entries.MoveNext()) { throw 'Fresh hosted Windows home required' } }
            finally { $entries.Dispose() }
        } elseif ($file.Exists) { throw 'Fresh hosted Windows home required' }
    }
}

function Initialize-DiagHostedWorker {
    param([string]$Directory)
    Assert-DiagHostedWorker
    Assert-DiagMetadataPath $Directory
    Assert-DiagHostEnvironment
    $listing = $null
    try {
        $listing = Start-DiagProcess -Arguments @('--list', '--quiet')
        $buffer = [char[]]::new(4097)
        $read = $listing.StandardOutput.ReadAsync($buffer, 0, $buffer.Length)
        $clock = [Diagnostics.Stopwatch]::StartNew()
        while ((-not $listing.HasExited -or -not $read.IsCompleted) -and (Get-DiagMilliseconds $clock) -lt 15000) {
            Start-Sleep -Milliseconds 100
        }
        if (-not $listing.HasExited -or -not $read.IsCompleted) {
            $listing.Kill()
            throw 'Cannot establish a fresh disposable distribution'
        }
        if ($listing.ExitCode -ne 0 -or $read.GetAwaiter().GetResult() -ne 0) {
            throw 'Preexisting or unavailable WSL distribution; no reuse'
        }
        Write-DiagJson $Directory 'hosted-ci.json' @{ schema = 1; mode = $script:DiagMode
            github_hosted_windows = $true; windows_worker = $true; distribution_was_absent = $true
            native_home_clean = $true; credential_environment_clear = $true }
    } finally { if ($null -ne $listing) { $listing.Dispose() } }
}

function Assert-DiagHostedMarker {
    param([string]$Directory)
    $row = Read-DiagJson (Join-Path $Directory 'hosted-ci.json')
    if ($null -eq $row) { throw 'Fresh hosted-worker marker required' }
    $names = ($row.Keys | Sort-Object) -join ','
    if ($names -cne 'credential_environment_clear,distribution_was_absent,github_hosted_windows,mode,native_home_clean,schema,windows_worker' -or
        ($row.schema -isnot [int] -and $row.schema -isnot [long]) -or
        $row.schema -ne 1 -or $row.mode -cne $script:DiagMode) { throw 'Invalid hosted-worker marker' }
    foreach ($name in @('credential_environment_clear', 'distribution_was_absent', 'github_hosted_windows', 'native_home_clean', 'windows_worker')) {
        if ($row[$name] -isnot [bool] -or -not $row[$name]) { throw 'Invalid hosted-worker marker' }
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
        $text = [Text.UTF8Encoding]::new($false, $true).GetString($buffer, 0, $count)
        $document = [System.Text.Json.JsonDocument]::Parse($text, [System.Text.Json.JsonDocumentOptions]::new())
        try {
            if ($document.RootElement.ValueKind -ne [System.Text.Json.JsonValueKind]::Object) { throw 'Invalid metadata' }
            $names = @($document.RootElement.EnumerateObject() | ForEach-Object { $_.Name })
            if (@($names | Sort-Object -Unique).Count -ne $names.Count) { throw 'Invalid metadata' }
        } finally { $document.Dispose() }
        return ($text | ConvertFrom-Json -AsHashtable)
    } finally { $stream.Dispose() }
}

function Get-DiagFixtureId {
    param([System.Text.Json.JsonElement]$Element)
    $value = 0L
    if ($Element.ValueKind -ne [System.Text.Json.JsonValueKind]::Number -or
        -not $Element.TryGetInt64([ref]$value) -or $value -lt 1 -or $value -gt 1000000) {
        throw 'Invalid fixture metadata'
    }
    return $value
}

function Test-DiagFixtureRecord {
    param([string]$Line)
    $document = [System.Text.Json.JsonDocument]::Parse($Line, [System.Text.Json.JsonDocumentOptions]::new())
    try {
        $root = $document.RootElement
        if ($root.ValueKind -ne [System.Text.Json.JsonValueKind]::Object) { throw 'Invalid fixture metadata' }
        # EnumerateObject retains duplicate keys; the exact key list rejects them.
        $names = @($root.EnumerateObject() | ForEach-Object { $_.Name })
        $keys = ($names | Sort-Object) -join ','
        if ($keys -cne 'carried,event,method,model,path_category,reply,stream') {
            throw 'Invalid fixture metadata'
        }
        foreach ($name in @('event', 'method', 'model', 'path_category')) {
            if ($root.GetProperty($name).ValueKind -ne [System.Text.Json.JsonValueKind]::String) {
                throw 'Invalid fixture metadata'
            }
        }
        if ($root.GetProperty('event').GetString() -cne 'request-parsed' -or
            $root.GetProperty('model').GetString() -cne 'fixture-model-1') { throw 'Invalid fixture metadata' }
        $stream = $root.GetProperty('stream').ValueKind
        if ($stream -notin @([System.Text.Json.JsonValueKind]::True, [System.Text.Json.JsonValueKind]::False)) {
            throw 'Invalid fixture metadata'
        }
        $carried = $root.GetProperty('carried')
        if ($carried.ValueKind -ne [System.Text.Json.JsonValueKind]::Array -or $carried.GetArrayLength() -gt 128) {
            throw 'Invalid fixture metadata'
        }
        $previous = 0L
        foreach ($item in $carried.EnumerateArray()) {
            $number = Get-DiagFixtureId $item
            if ($number -le $previous) { throw 'Invalid fixture metadata' }
            $previous = $number
        }
        $method = $root.GetProperty('method').GetString()
        $path = $root.GetProperty('path_category').GetString()
        if ($method -ceq 'GET') {
            if ($path -cne 'models' -or $stream -ne [System.Text.Json.JsonValueKind]::False -or
                $root.GetProperty('reply').ValueKind -ne [System.Text.Json.JsonValueKind]::Null -or
                $carried.GetArrayLength() -ne 0) { throw 'Invalid fixture metadata' }
            return $false
        }
        if ($method -cne 'POST' -or $path -cne 'chat-completions') { throw 'Invalid fixture metadata' }
        $reply = Get-DiagFixtureId ($root.GetProperty('reply'))
        return $reply -eq 1
    } finally { $document.Dispose() }
}

function Test-DiagFirstFixtureRequest {
    param([string]$Directory)
    $path = Join-Path $Directory 'fixture.jsonl' # Only this Windows-local metadata file.
    $file = [IO.FileInfo]::new($path)
    if (-not $file.Exists) { return $false }
    $limit = 1048576
    if ($file.Length -gt $limit -or ($file.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'Invalid fixture metadata'
    }
    $stream = [IO.File]::Open($path, [IO.FileMode]::Open, [IO.FileAccess]::Read, ([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete))
    try {
        $buffer = [byte[]]::new($limit + 1)
        $count = 0
        while ($count -lt $buffer.Length) {
            $read = $stream.Read($buffer, $count, $buffer.Length - $count)
            if ($read -eq 0) { break }
            $count += $read
        }
        if ($count -gt $limit) { throw 'Invalid fixture metadata' }
    } finally { $stream.Dispose() }
    $decoder = [Text.UTF8Encoding]::new($false, $true)
    $start = 0
    $records = 0
    $found = $false
    for ($i = 0; $i -lt $count; $i++) {
        if ($buffer[$i] -ne 10) { continue }
        if ($i - $start -gt 4096 -or $records -ge 256) { throw 'Invalid fixture metadata' }
        $line = $decoder.GetString($buffer, $start, $i - $start)
        if (Test-DiagFixtureRecord $line) { $found = $true }
        $records++
        $start = $i + 1
    }
    $partial = $count - $start
    if ($partial -gt 4096 -or ($partial -gt 0 -and $records -ge 256)) { throw 'Invalid fixture metadata' }
    # An append can be observed before its newline (even inside UTF-8). Wait
    # for the complete record; never parse or arm from an incomplete tail.
    if ($partial -gt 0) { return $false }
    return $found
}

function Test-DiagObserverEnd {
    param([string]$Directory)
    $row = Read-DiagJson (Join-Path $Directory 'observer.json')
    if ($null -eq $row) { return $false }
    $names = ($row.Keys | Sort-Object) -join ','
    if ($names -cne 'end_captured,mode,schema' -or ($row.schema -isnot [int] -and $row.schema -isnot [long]) -or
        $row.schema -ne 1 -or $row.mode -cne $script:DiagMode -or
        $row.end_captured -isnot [bool] -or -not $row.end_captured) { throw 'Invalid observer metadata' }
    return $true
}

function Write-DiagJson {
    param([string]$Directory, [ValidateSet('watchdog.json', 'hosted-ci.json', 'capture-request.json', 'cleanup.json')][string]$Name, [hashtable]$Row)
    $path = Join-Path $Directory $Name
    $pending = "$path.pending"
    [IO.File]::WriteAllText($pending, (($Row | ConvertTo-Json -Depth 4 -Compress) + "`n"), [Text.UTF8Encoding]::new($false))
    [IO.File]::Move($pending, $path, $true)
}

function Write-DiagResult {
    param([string]$Directory, [hashtable]$Row)
    Write-DiagJson $Directory 'watchdog.json' $Row
}

function Request-DiagEndCapture {
    param([string]$Directory)
    Write-DiagJson $Directory 'capture-request.json' @{ schema = 1; mode = $script:DiagMode; stop = $true }
    $clock = [Diagnostics.Stopwatch]::StartNew()
    while ((Get-DiagMilliseconds $clock) -lt 2000) {
        if (Test-DiagObserverEnd $Directory) { return $true }
        Start-Sleep -Milliseconds 100
    }
    return $false # Honest incomplete end capture; never delay teardown indefinitely.
}

function Stop-DiagDistribution {
    Assert-DiagHostedWorker
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
    param([string]$Directory, [string]$LinuxDirectory, [string]$Workspace)
    Assert-DiagHostedWorker
    Assert-DiagMetadataPath $Directory
    if ($LinuxDirectory -notmatch '^/mnt/[a-z]/[A-Za-z0-9/_. -]+$' -or
        $Workspace -notmatch '^/mnt/[a-z]/[A-Za-z0-9/_. -]+/candidate$') { throw 'Invalid hosted workspace path' }
    $clock = [Diagnostics.Stopwatch]::StartNew()
    $process = $null
    $owned = $false
    $armedAt = -1L
    $row = @{ schema = 1; mode = $script:DiagMode; status = 'launch-failed'; elapsed_ms = 0L; armed_at_ms = $null
        arm_basis = 'first-fixture-request'; launcher_exit_code = $null
        capture_confirmed = $false; cleanup_confirmed = $false; terminate_state = 'pending' }
    try {
        Assert-DiagHostedMarker $Directory
        $owned = $true
        Assert-DiagHostEnvironment
        # Default ordinary user, Windows-checkout parent CWD, inherited environment.
        # The shell starts an observer sibling and the original foreground journey.
        $process = Start-DiagProcess -Arguments @('--distribution', 'Ubuntu-24.04', '--cd', $Workspace, '--exec',
            'sh', '/home/journey/diag/wsl_diag.sh', 'journey', $LinuxDirectory, $Workspace)
        $row.status = 'watchdog-error'
        while ($true) {
            $elapsed = Get-DiagMilliseconds $clock
            $deadline = Get-DiagDeadline $elapsed $armedAt
            if ($deadline) { $row.status = $deadline; break }
            if ($armedAt -lt 0) {
                try {
                    if (Test-DiagFirstFixtureRequest $Directory) { $armedAt = $elapsed; $row.armed_at_ms = $armedAt }
                } catch { $row.status = 'invalid-metadata'; break }
            }
            if ($process.HasExited) {
                $row.launcher_exit_code = $process.ExitCode
                $row.status = 'completed'
                break
            }
            Start-Sleep -Milliseconds 250
        }
    } catch {
        if ($row.status -ne 'launch-failed') { $row.status = 'watchdog-error' }
    } finally {
        $row.elapsed_ms = Get-DiagMilliseconds $clock
        try {
            # Persist the decision and request bounded end capture BEFORE teardown.
            Write-DiagResult $Directory $row
            if ($owned) {
                try { $row.capture_confirmed = Request-DiagEndCapture $Directory } catch { $row.capture_confirmed = $false }
            }
        } finally {
            # Success also terminates the fresh distro. Launcher exit alone proves nothing.
            if ($owned) { $row.terminate_state = Stop-DiagDistribution }
            else { $row.terminate_state = 'not-owned' }
            $row.cleanup_confirmed = $row.terminate_state -eq 'returned'
            if ($null -ne $process) {
                try { if (-not $process.HasExited) { $process.Kill() } } catch { }
                $process.Dispose()
            }
            Write-DiagResult $Directory $row
        }
    }
    if ($row.status -eq 'completed' -and $row.launcher_exit_code -eq 0 -and
        $row.capture_confirmed -and $row.cleanup_confirmed) { return 0 }
    return 1
}

if ($MyInvocation.InvocationName -ne '.') {
    exit (Invoke-DiagWatchdog -Directory $MetadataDirectory -LinuxDirectory $LinuxMetadata -Workspace $LinuxWorkspace)
}
