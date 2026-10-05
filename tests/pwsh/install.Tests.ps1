# Pester 5 tests for packaging/install.ps1 with a stubbed wsl.exe.
#
#   pwsh -NoProfile -Command "Invoke-Pester -Path tests/pwsh -Output Detailed"
#
# Calls to WSL use mocked wrappers, except the native exit-status tests,
# which run a local installer stub, so no test needs WSL itself.

BeforeAll {
    . "$PSScriptRoot/../../packaging/install.ps1"

    function New-WslResult {
        param([int]$ExitCode = 0, [string]$Output = '')
        [pscustomobject]@{ ExitCode = $ExitCode; Output = $Output }
    }

    $script:Listing = "  NAME            STATE           VERSION`n* Ubuntu          Running         2`n  Legacy          Stopped         1`n"
    $script:Url = 'https://example.invalid/releases/v1.0.0/install.sh'
    $script:Checksum = 'a' * 64
}

Describe 'Get-WslDistribution' {
    It 'parses wsl --list --verbose' {
        Mock Invoke-Wsl { New-WslResult -Output $script:Listing }
        $rows = @(Get-WslDistribution)
        $rows.Count | Should -Be 2
        $rows[0].Name | Should -Be 'Ubuntu'
        $rows[0].IsDefault | Should -BeTrue
        $rows[0].Version | Should -Be 2
        $rows[1].Name | Should -Be 'Legacy'
        $rows[1].IsDefault | Should -BeFalse
        $rows[1].Version | Should -Be 1
    }

    It 'returns nothing when no distribution is installed' {
        Mock Invoke-Wsl { New-WslResult -ExitCode -1 -Output 'Windows Subsystem for Linux has no installed distributions.' }
        @(Get-WslDistribution).Count | Should -Be 0
    }
}

Describe 'Invoke-Main' {
    BeforeEach {
        Mock Get-ReleaseInfo {
            [pscustomobject]@{ Version = '1.0.0'; InstallerUrl = $script:Url; InstallerSha256 = $script:Checksum }
        }
        Mock Test-WindowsHost { $true }
        Mock Get-WindowsBuild { 22631 }
        Mock Write-Host { }
        Mock Test-WslReady { $true }
        Mock Invoke-Wsl { New-WslResult -Output $script:Listing } -ParameterFilter { $Arguments -contains '--list' }
        Mock Invoke-WslInteractive { $global:LASTEXITCODE = 0 }
    }

    It 'runs the Linux installer inside the default WSL 2 distribution, then setup' {
        Invoke-Main -Version '1.0.0' -InstallerUrl $script:Url
        $script:MainExitCode | Should -Be 0
        Should -Invoke Invoke-WslInteractive -Times 1 -Exactly -ParameterFilter {
            $Arguments[0] -eq '--distribution' -and $Arguments[1] -eq 'Ubuntu' -and $Arguments[2] -eq '--exec' -and
            $Arguments -contains $script:Url -and $Arguments -contains '1.0.0' -and $Arguments -contains $script:Checksum
        }
        Should -Invoke Invoke-WslInteractive -Times 1 -Exactly -ParameterFilter { $Arguments -contains 'setup' }
    }

    It 'skips setup with -NoSetup' {
        Invoke-Main -Version '1.0.0' -InstallerUrl $script:Url -NoSetup
        $script:MainExitCode | Should -Be 0
        Should -Invoke Invoke-WslInteractive -Times 0 -Exactly -ParameterFilter { $Arguments -contains 'setup' }
    }

    It 'refuses an unchecked override before preparing WSL' {
        Mock Test-WslReady { throw 'WSL must not be touched' }
        { Invoke-Main -Version '2.0.0' -InstallerUrl 'https://example.invalid/releases/v2.0.0/install.sh' } | Should -Throw '*trusted*InstallerSha256*'
        Should -Invoke Test-WslReady -Times 0 -Exactly
        Should -Invoke Invoke-WslInteractive -Times 0 -Exactly
    }

    It 'refuses a WSL 1 distribution' {
        { Invoke-Main -Distribution 'Legacy' -Version '1.0.0' -InstallerUrl $script:Url } | Should -Throw '*WSL 1*wsl --set-version Legacy 2*'
        Should -Invoke Invoke-WslInteractive -Times 0 -Exactly
    }

    It 'refuses Windows builds without WSL 2' {
        Mock Get-WindowsBuild { 18363 }
        { Invoke-Main -Version '1.0.0' -InstallerUrl $script:Url } | Should -Throw '*build 19041*'
    }

    It 'refuses to run outside Windows' {
        Mock Test-WindowsHost { $false }
        { Invoke-Main -Version '1.0.0' -InstallerUrl $script:Url } | Should -Throw '*run install.sh*'
    }

    It 'asks before installing WSL 2 and changes nothing on no' {
        Mock Test-WslReady { $false }
        Mock Read-Host { 'n' }
        Mock Invoke-Wsl { New-WslResult } -ParameterFilter { $Arguments -contains '--install' }
        { Invoke-Main -Version '1.0.0' -InstallerUrl $script:Url } | Should -Throw '*wsl --install*'
        Should -Invoke Invoke-Wsl -Times 0 -Exactly -ParameterFilter { $Arguments -contains '--install' }
    }

    It 'installs WSL 2 with consent as administrator and asks for a restart' {
        Mock Test-WslReady { $false }
        Mock Test-Administrator { $true }
        Mock Invoke-Wsl { New-WslResult } -ParameterFilter { $Arguments -contains '--install' }
        Invoke-Main -Version '1.0.0' -InstallerUrl $script:Url -AssumeYes
        $script:MainExitCode | Should -Be 3
        Should -Invoke Invoke-Wsl -Times 1 -Exactly -ParameterFilter { $Arguments -contains '--no-distribution' }
    }

    It 'needs administrator rights to install WSL 2' {
        Mock Test-WslReady { $false }
        Mock Test-Administrator { $false }
        { Invoke-Main -Version '1.0.0' -InstallerUrl $script:Url -AssumeYes } | Should -Throw '*administrator*'
    }

    It 'offers Ubuntu when no distribution exists' {
        Mock Invoke-Wsl { New-WslResult -ExitCode -1 -Output 'no installed distributions' } -ParameterFilter { $Arguments -contains '--list' }
        Mock Invoke-Wsl { New-WslResult } -ParameterFilter { $Arguments -contains '--install' }
        Invoke-Main -Version '1.0.0' -InstallerUrl $script:Url -AssumeYes
        $script:MainExitCode | Should -Be 3
        Should -Invoke Invoke-Wsl -Times 1 -Exactly -ParameterFilter { $Arguments -contains 'Ubuntu' }
    }

    It 'reports a failed Linux installer' {
        Mock Invoke-WslInteractive { $global:LASTEXITCODE = 1 }
        { Invoke-Main -Version '1.0.0' -InstallerUrl $script:Url } | Should -Throw '*Linux installer failed*'
    }
}

Describe 'Installer output and native exit status' {
    BeforeAll {
        # Exercise the real interactive wrapper, with a native installer stub
        # in place of WSL. Native stdout must reach the host, not its caller.
        function wsl.exe {
            & (Get-Process -Id $PID).Path -NoProfile -File $script:InstallerStub
            $global:LASTEXITCODE = $LASTEXITCODE
        }
    }

    BeforeEach {
        $script:InstallerStub = Join-Path $TestDrive 'installer.ps1'
        Mock Get-ReleaseInfo {
            [pscustomobject]@{ Version = '1.0.0'; InstallerUrl = $script:Url; InstallerSha256 = $script:Checksum }
        }
        Mock Test-WindowsHost { $true }
        Mock Get-WindowsBuild { 22631 }
        Mock Write-Host { }
        Mock Test-WslReady { $true }
        Mock Invoke-Wsl { New-WslResult -Output $script:Listing }
    }

    It 'succeeds when the installer prints lines and exits 0' {
        Set-Content $script:InstallerStub "Write-Output 'verified signature', 'installed claude-multi 1.0.0'; exit 0"
        Invoke-Main -Version '1.0.0' -InstallerUrl $script:Url -NoSetup
        $script:MainExitCode | Should -Be 0
    }

    It 'reports exit 3 when the installer prints lines and exits 3' {
        Set-Content $script:InstallerStub "Write-Output 'verified signature', 'installation refused'; exit 3"
        { Invoke-Main -Version '1.0.0' -InstallerUrl $script:Url -NoSetup } |
            Should -Throw '*The Linux installer failed in Ubuntu (exit 3).*'
    }
}

Describe 'Script entry point with native installer output' {
    It 'handles printed installer output and exit <NativeExit> at the entry point' -ForEach @(
        @{ NativeExit = 0; ScriptExit = 0 },
        @{ NativeExit = 3; ScriptExit = 1 }
    ) {
        $installer = (Resolve-Path "$PSScriptRoot/../../packaging/install.ps1").Path
        $text = Get-Content $installer -Raw
        $entry = $text.Substring($text.IndexOf('if ($MyInvocation.InvocationName -ne ''.'')'))
        $stub = Join-Path $TestDrive 'native-installer.ps1'
        Set-Content $stub "Write-Output 'verified signature', 'installer output'; exit $NativeExit"
        # Keep the shipped entry point intact; replace only platform discovery
        # and the external WSL executable in this child-process driver.
        $driver = Join-Path $TestDrive 'entry.ps1'
        $prefix = @'
param([string]$Installer, [string]$Stub)
. $Installer
function Test-WindowsHost { $true }
function Get-WindowsBuild { 22631 }
function Test-WslReady { $true }
function Invoke-Wsl {
    [pscustomobject]@{ ExitCode = 0; Output = "  NAME STATE VERSION`n* Ubuntu Running 2`n" }
}
function wsl.exe {
    & (Get-Process -Id $PID).Path -NoProfile -File $Stub
    $global:LASTEXITCODE = $LASTEXITCODE
}
$Version = '1.0.0'
$InstallerUrl = 'https://example.invalid/install.sh'
$InstallerSha256 = 'a' * 64
$NoSetup = $true
'@
        Set-Content $driver ($prefix + "`n" + $entry)
        $output = & (Get-Process -Id $PID).Path -NoProfile -File $driver -Installer $installer -Stub $stub 2>&1 | Out-String
        $LASTEXITCODE | Should -Be $ScriptExit
        $output | Should -Match 'verified signature'
        $output | Should -Match 'installer output'
        if ($NativeExit -eq 3) {
            $output | Should -Match 'The Linux installer failed in Ubuntu \(exit 3\)'
        }
    }
}

Describe 'Get-InstallerSource and Get-BootstrapArgument' {
    It 'needs a release or explicit values in a source-tree copy' {
        { Get-InstallerSource -Version '1.0.0' } | Should -Throw '*names no release*'
    }

    It 'refuses plain http, bad versions and bad checksums' {
        { Get-InstallerSource -Version '1.0.0' -InstallerUrl 'http://example.invalid/install.sh' } | Should -Throw '*https*'
        { Get-InstallerSource -Version '1.0' -InstallerUrl $script:Url } | Should -Throw '*release version*'
        { Get-InstallerSource -Version '1.0.0' -InstallerUrl $script:Url -InstallerSha256 'xyz' } | Should -Throw '*sha256*'
    }

    It 'passes the bootstrap as base64 and refuses unsafe installer arguments' {
        $source = Get-InstallerSource -Version '1.0.0' -InstallerUrl $script:Url -InstallerSha256 $script:Checksum
        $arguments = Get-BootstrapArgument -Source $source -InstallerArguments @('--modify-path')
        $arguments[0..2] | Should -Be @('sh', '-c', 'echo $0 | base64 -d | sh -s -- $@')
        $decoded = [Text.Encoding]::ASCII.GetString([Convert]::FromBase64String($arguments[3]))
        $decoded | Should -BeLike '*sh "$file" --no-setup "$@" </dev/null*'
        $arguments.Count | Should -Be 9
        $arguments[4..8] | Should -Be @($script:Url, $script:Checksum, '--version', '1.0.0', '--modify-path')
        { Get-BootstrapArgument -Source $source -InstallerArguments @('a b') } | Should -Throw '*Unsupported*'
    }
}

Describe 'Embedded installer checksum binding' {
    BeforeEach {
        Mock Get-ReleaseInfo {
            [pscustomobject]@{ Version = '1.0.0'; InstallerUrl = $script:Url; InstallerSha256 = $script:Checksum }
        }
    }

    It 'uses the embedded checksum for the exact embedded URL even with another requested version' {
        foreach ($version in @('1.0.0', '2.0.0')) {
            $source = Get-InstallerSource -Version $version
            $source.Url | Should -BeExactly $script:Url
            $source.Sha256 | Should -BeExactly $script:Checksum
            (Get-BootstrapArgument -Source $source)[5] | Should -BeExactly $script:Checksum
        }
    }

    It 'does not reuse the checksum for another URL or differently cased path' {
        foreach ($url in @('https://example.invalid/other/install.sh', $script:Url.Replace('install.sh', 'INSTALL.sh'))) {
            { Get-InstallerSource -Version '1.0.0' -InstallerUrl $url } | Should -Throw '*trusted*InstallerSha256*'
        }
    }

    It 'requires an explicit checksum when a version changes a templated URL' {
        Mock Get-ReleaseInfo {
            [pscustomobject]@{ Version = '1.0.0'; InstallerUrl = 'https://example.invalid/releases/v{version}/install.sh'; InstallerSha256 = $script:Checksum }
        }
        (Get-InstallerSource).Sha256 | Should -BeExactly $script:Checksum
        { Get-InstallerSource -Version '2.0.0' } | Should -Throw '*trusted*InstallerSha256*'
        $source = Get-InstallerSource -Version '2.0.0' -InstallerSha256 ('b' * 64)
        $source.Url | Should -BeExactly 'https://example.invalid/releases/v2.0.0/install.sh'
        $source.Sha256 | Should -BeExactly ('b' * 64)
    }

    It 'accepts an explicit checksum for an alternate URL' {
        $source = Get-InstallerSource -InstallerUrl 'https://example.invalid/other/install.sh' -InstallerSha256 ('b' * 64)
        $source.Sha256 | Should -BeExactly ('b' * 64)
    }

    It 'refuses an unchecked or malformed bootstrap source' {
        foreach ($sum in @('', 'none', ('g' * 64))) {
            $source = [pscustomobject]@{ Version = '1.0.0'; Url = $script:Url; Sha256 = $sum }
            { Get-BootstrapArgument -Source $source } | Should -Throw '*trusted*sha256*'
        }
    }
}

Describe 'Release placeholders' {
    It 'are empty in the source tree' {
        $release = Get-ReleaseInfo
        $release.Version | Should -Be ''
        $release.InstallerUrl | Should -Be ''
        $release.InstallerSha256 | Should -Be ''
    }
}
