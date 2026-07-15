# WorkBuddy Copilot Windows installer.  It never guesses WorkBuddy paths and
# never accepts a plaintext token as a command-line argument.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string]$ProjectRoot,
    [Parameter(Mandatory = $true)] [string]$ConfigDir,
    [Parameter(Mandatory = $true)] [string]$ProfilePath,
    [Parameter(Mandatory = $true)] [string]$StudentId,
    [Parameter(Mandatory = $true)] [string]$GitBashHookCommand,
    [Parameter(Mandatory = $true)] [string]$BaseUrl,
    [string]$PythonCommand = 'py',
    [string]$TokenFile = '',
    [string]$SpoolDir = '',
    [string]$StateDir = '',
    [string]$LogDir = '',
    [switch]$ValidateSecurityOnly,
    [switch]$PrepareRuntimeStateOnly
)

$ErrorActionPreference = 'Stop'
$OwnerId = 'workbuddy-copilot-v1'

function Test-FullyQualifiedPath([string]$Path) {
    if ([string]::IsNullOrWhiteSpace($Path)) {
        return $false
    }
    if ($Path -match '^[A-Za-z]:[\\/]') {
        return $true
    }
    return $Path -match '^\\\\[^\\/:*?"<>|]+\\[^\\/:*?"<>|]+(?:\\|$)'
}

function Initialize-WindowsSecurityModule {
    $builtInModulePath = Join-Path $PSHOME 'Modules'
    $modulePaths = @(
        $env:PSModulePath -split ';' |
            Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    )
    if ($modulePaths -notcontains $builtInModulePath) {
        $env:PSModulePath = (@($builtInModulePath) + $modulePaths) -join ';'
    }
    Import-Module Microsoft.PowerShell.Security -ErrorAction Stop
}

Initialize-WindowsSecurityModule

function Require-Directory([string]$Path, [string]$Name) {
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        throw "$Name does not exist: $Path"
    }
}

function Require-File([string]$Path, [string]$Name) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Name does not exist: $Path"
    }
}

function Assert-AbsolutePath([string]$Path, [string]$Name) {
    if (-not (Test-FullyQualifiedPath $Path)) {
        throw "$Name must be an absolute path"
    }
}

function Get-CurrentUserSid {
    return [System.Security.Principal.WindowsIdentity]::GetCurrent().User
}

function Protect-PrivatePath([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "cannot protect missing private path: $Path"
    }
    $item = Get-Item -LiteralPath $Path -Force
    $sid = Get-CurrentUserSid
    $acl = Get-Acl -LiteralPath $item.FullName
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($rule in @($acl.Access)) {
        [void]$acl.RemoveAccessRuleAll($rule)
    }
    $acl.SetOwner($sid)
    $inheritance = [System.Security.AccessControl.InheritanceFlags]::None
    if ($item.PSIsContainer) {
        $inheritance = (
            [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
            [System.Security.AccessControl.InheritanceFlags]::ObjectInherit
        )
    }
    $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
        $sid,
        [System.Security.AccessControl.FileSystemRights]::FullControl,
        $inheritance,
        [System.Security.AccessControl.PropagationFlags]::None,
        [System.Security.AccessControl.AccessControlType]::Allow
    )
    $acl.SetAccessRule($rule)
    Set-Acl -LiteralPath $item.FullName -AclObject $acl
}

function Assert-PrivateAcl([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "private ACL path is missing: $Path"
    }
    $sid = Get-CurrentUserSid
    $acl = Get-Acl -LiteralPath $Path
    if (-not $acl.AreAccessRulesProtected) {
        throw "private ACL inheritance is enabled: $Path"
    }
    try {
        $ownerSid = (New-Object System.Security.Principal.NTAccount($acl.Owner)).Translate(
            [System.Security.Principal.SecurityIdentifier]
        )
    } catch {
        $ownerSid = New-Object System.Security.Principal.SecurityIdentifier($acl.Owner)
    }
    if ($ownerSid.Value -ne $sid.Value) {
        throw "private ACL owner mismatch: $Path"
    }
    $hasCurrentUserAllow = $false
    $rules = $acl.GetAccessRules(
        $true,
        $true,
        [System.Security.Principal.SecurityIdentifier]
    )
    foreach ($rule in $rules) {
        if ($rule.IsInherited) {
            throw "private ACL contains inherited entries: $Path"
        }
        if ($rule.AccessControlType -eq [System.Security.AccessControl.AccessControlType]::Allow) {
            if ($rule.IdentityReference.Value -ne $sid.Value) {
                throw "private ACL grants another identity: $Path"
            }
            $hasCurrentUserAllow = $true
        }
    }
    if (-not $hasCurrentUserAllow) {
        throw "private ACL does not grant the current user: $Path"
    }
}

function ConvertTo-NativeArgument([string]$Value) {
    if ($Value -notmatch '[\s"]') { return $Value }
    $escaped = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
    $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
    return '"' + $escaped + '"'
}

function Get-Sha256([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Assert-PathInside([string]$Child, [string]$Parent) {
    $separator = [System.IO.Path]::DirectorySeparatorChar
    $parentFull = [System.IO.Path]::GetFullPath($Parent).TrimEnd('\', '/')
    $childFull = [System.IO.Path]::GetFullPath($Child).TrimEnd('\', '/')
    $prefix = $parentFull + $separator
    if ($childFull -ne $parentFull -and
        -not $childFull.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "owned path escapes state directory: $Child"
    }
}

function Assert-NoReparsePoint([string]$Path) {
    $item = Get-Item -LiteralPath $Path -Force
    if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "reparse/junction path is not allowed: $Path"
    }
}

function Assert-NoReparsePointChain([string]$Path) {
    $current = [System.IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
    while (-not [string]::IsNullOrWhiteSpace($current)) {
        if (Test-Path -LiteralPath $current) {
            Assert-NoReparsePoint $current
        }
        $parent = [System.IO.Directory]::GetParent($current)
        if ($null -eq $parent -or $parent.FullName -eq $current) { break }
        $current = $parent.FullName.TrimEnd('\', '/')
    }
}

function Write-BytesAtomically([string]$Path, [byte[]]$Bytes) {
    $directory = Split-Path -LiteralPath $Path -Parent
    Require-Directory $directory 'atomic destination directory'
    $temporary = Join-Path $directory ('.{0}.{1}.tmp' -f (Split-Path -Leaf $Path), [Guid]::NewGuid().ToString('N'))
    try {
        [System.IO.File]::WriteAllBytes($temporary, $Bytes)
        Protect-PrivatePath $temporary
        Assert-PrivateAcl $temporary
        if (Test-Path -LiteralPath $Path -PathType Leaf) {
            [System.IO.File]::Replace($temporary, $Path, $null, $true)
        } else {
            Move-Item -LiteralPath $temporary -Destination $Path
        }
        Protect-PrivatePath $Path
        Assert-PrivateAcl $Path
    } finally {
        if (Test-Path -LiteralPath $temporary -PathType Leaf) {
            Remove-Item -LiteralPath $temporary -Force
        }
    }
}

function Write-PrivateTextAtomically([string]$Path, [string]$Text) {
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    Write-BytesAtomically $Path ($utf8.GetBytes($Text))
}

function Write-JsonAtomically([string]$Path, [object]$Value) {
    $json = $Value | ConvertTo-Json -Depth 20
    Write-PrivateTextAtomically $Path ($json + [Environment]::NewLine)
}

function Copy-PrivateFileAtomically([string]$Source, [string]$Destination) {
    Write-BytesAtomically $Destination ([System.IO.File]::ReadAllBytes($Source))
}

function Restore-SettingsAtomically([string]$BackupPath, [string]$SettingsPath) {
    $directory = Split-Path -LiteralPath $SettingsPath -Parent
    $temporary = Join-Path $directory ('.settings.rollback.{0}.tmp' -f [Guid]::NewGuid().ToString('N'))
    try {
        [System.IO.File]::WriteAllBytes(
            $temporary,
            [System.IO.File]::ReadAllBytes($BackupPath)
        )
        [System.IO.File]::Replace($temporary, $SettingsPath, $null, $true)
    } finally {
        if (Test-Path -LiteralPath $temporary -PathType Leaf) {
            Remove-Item -LiteralPath $temporary -Force
        }
    }
}

function Remove-OwnedHookEntries([object]$Settings, [string]$EntryOwner, [switch]$IncludeLegacy) {
    if ($null -eq $Settings.hooks) { return $Settings }
    foreach ($eventProperty in @($Settings.hooks.PSObject.Properties)) {
        $retainedBlocks = New-Object System.Collections.ArrayList
        foreach ($block in @($eventProperty.Value)) {
            $retainedHooks = New-Object System.Collections.ArrayList
            foreach ($hook in @($block.hooks)) {
                $command = [string]$hook.command
                $owned = $command.Contains("COPILOT_ENTRY_OWNER=$EntryOwner")
                $legacy = $IncludeLegacy -and $command.Contains('copilot/hook.py')
                if (-not $owned -and -not $legacy) {
                    [void]$retainedHooks.Add($hook)
                }
            }
            if ($retainedHooks.Count -gt 0) {
                $block.hooks = @($retainedHooks)
                [void]$retainedBlocks.Add($block)
            }
        }
        $eventProperty.Value = @($retainedBlocks)
    }
    return $Settings
}

function Read-InteractiveToken {
    $secure = Read-Host 'Student token' -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
}

function Invoke-Python313([string[]]$Arguments) {
    if ($PythonCommand -eq 'py') {
        & $PythonCommand -3.13 @Arguments
    } else {
        & $PythonCommand @Arguments
    }
    if ($LASTEXITCODE -ne 0) {
        throw "Python 3.13 command failed with exit code $LASTEXITCODE"
    }
}

function Restore-PreviousVenv(
    [string]$VenvDir,
    [string]$RollbackDir,
    [bool]$HadPreviousVenv
) {
    if (Test-Path -LiteralPath $VenvDir -PathType Container) {
        Remove-Item -LiteralPath $VenvDir -Recurse -Force
    }
    if ($HadPreviousVenv) {
        if (-not (Test-Path -LiteralPath $RollbackDir -PathType Container)) {
            throw 'previous Windows virtual environment rollback copy is missing'
        }
        Move-Item -LiteralPath $RollbackDir -Destination $VenvDir
    }
}

Assert-AbsolutePath $ProjectRoot 'ProjectRoot'
Assert-AbsolutePath $ConfigDir 'ConfigDir'
Assert-AbsolutePath $ProfilePath 'ProfilePath'
Require-Directory $ProjectRoot 'ProjectRoot'
Require-Directory $ConfigDir 'ConfigDir'
Require-File $ProfilePath 'ProfilePath'
Assert-NoReparsePointChain $ProjectRoot
Assert-NoReparsePointChain $ConfigDir
Assert-NoReparsePointChain $ProfilePath
$settingsPath = Join-Path $ConfigDir 'settings.json'
Require-File $settingsPath 'settings.json'
if ([string]::IsNullOrWhiteSpace($GitBashHookCommand)) {
    throw 'Installation requires W0-verified Git Bash hook command; no command was constructed.'
}

if ($ValidateSecurityOnly) {
    if ([string]::IsNullOrWhiteSpace($TokenFile)) {
        throw 'ValidateSecurityOnly requires TokenFile'
    }
    Require-File $TokenFile 'TokenFile'
    Assert-PrivateAcl $TokenFile
    Write-Output 'private ACL validation passed'
    return
}

if ([string]::IsNullOrWhiteSpace($StateDir) -or [string]::IsNullOrWhiteSpace($LogDir)) {
    throw 'StateDir and LogDir are required for a normal installation'
}
Assert-AbsolutePath $StateDir 'StateDir'
Assert-AbsolutePath $LogDir 'LogDir'
if ($SpoolDir) { Assert-AbsolutePath $SpoolDir 'SpoolDir' }
if ($TokenFile) { Assert-AbsolutePath $TokenFile 'TokenFile' }

# No filesystem mutation precedes the interpreter and external TokenFile
# security gates. An unsupported Python or permissive token source leaves no
# half-created state directory behind.
Invoke-Python313 @('-c', 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 13) else 13)')
if ([string]::IsNullOrWhiteSpace($TokenFile)) {
    $token = Read-InteractiveToken
} else {
    Require-File $TokenFile 'TokenFile'
    Assert-PrivateAcl $TokenFile
    $token = [System.IO.File]::ReadAllText($TokenFile, [System.Text.Encoding]::UTF8).Trim()
}
if ([string]::IsNullOrWhiteSpace($token) -or $token.Length -gt 4096) {
    throw 'student token is empty or invalid'
}
Assert-NoReparsePointChain $StateDir
New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
Assert-NoReparsePoint $StateDir
Protect-PrivatePath $StateDir
Assert-PrivateAcl $StateDir

if ([string]::IsNullOrWhiteSpace($SpoolDir)) {
    $SpoolDir = Join-Path $StateDir 'spool'
}
Assert-PathInside $LogDir $StateDir
Assert-PathInside $SpoolDir $StateDir
Assert-NoReparsePointChain $SpoolDir
Assert-NoReparsePointChain $LogDir
New-Item -ItemType Directory -Force -Path $SpoolDir | Out-Null
Assert-NoReparsePoint $SpoolDir
Protect-PrivatePath $SpoolDir
Assert-PrivateAcl $SpoolDir
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
Assert-NoReparsePoint $LogDir
Protect-PrivatePath $LogDir
Assert-PrivateAcl $LogDir

$runtimeTokenPath = Join-Path $StateDir 'student.token'
Write-PrivateTextAtomically $runtimeTokenPath ($token + [Environment]::NewLine)
Protect-PrivatePath $runtimeTokenPath
Assert-PrivateAcl $runtimeTokenPath
$token = $null

$installId = [Guid]::NewGuid().ToString('N')
$runtimeConfigPath = Join-Path $StateDir 'client-config.json'
$instanceDigest = [System.BitConverter]::ToString(
    [System.Security.Cryptography.SHA256]::Create().ComputeHash(
        [System.Text.Encoding]::UTF8.GetBytes($StudentId)
    )
).Replace('-', '').Substring(0, 16).ToLowerInvariant()
$runtimeConfig = [ordered]@{
    schema_version = 1
    base_url = $BaseUrl.TrimEnd('/')
    student_id = $StudentId
    state_dir = $StateDir
    spool_dir = $SpoolDir
    log_dir = $LogDir
    token_file = $runtimeTokenPath
    workbuddy_config_dir = $ConfigDir
    workbuddy_profile = $ProfilePath
    single_instance_name = "WorkBuddyCopilot-$instanceDigest"
    install_id = $installId
    interval = 1.0
    bridge_timeout = 5.0
    heartbeat_interval = 2.0
}
Write-JsonAtomically $runtimeConfigPath $runtimeConfig
Protect-PrivatePath $runtimeConfigPath
Assert-PrivateAcl $runtimeConfigPath

$baselineBackupPath = Join-Path $StateDir "settings-baseline-$installId.json"
$settingsRollbackPath = Join-Path $StateDir "settings-rollback-$installId.json"
Copy-PrivateFileAtomically $settingsPath $settingsRollbackPath
Protect-PrivatePath $settingsRollbackPath
Assert-PrivateAcl $settingsRollbackPath
$settingsObject = Get-Content -LiteralPath $settingsPath -Raw -Encoding UTF8 | ConvertFrom-Json
$baselineObject = Remove-OwnedHookEntries $settingsObject $OwnerId -IncludeLegacy
Write-JsonAtomically $baselineBackupPath $baselineObject
Protect-PrivatePath $baselineBackupPath
Assert-PrivateAcl $baselineBackupPath
$settingsBeforeSha256 = Get-Sha256 $settingsPath
$baselineBackupSha256 = Get-Sha256 $baselineBackupPath

if ($PrepareRuntimeStateOnly) {
    Write-Output "Runtime state prepared: $runtimeConfigPath"
    return
}

$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$taskName = "WorkBuddyCopilot-$instanceDigest"
$venvDir = Join-Path $ProjectRoot '.venv-win'
$venvStagingDir = Join-Path $ProjectRoot ".venv-win.staging-$installId"
$venvRollbackDir = Join-Path $ProjectRoot ".venv-win.rollback-$installId"
Invoke-Python313 @('-m', 'venv', $venvStagingDir)
$venvStagingPython = Join-Path $venvStagingDir 'Scripts\python.exe'
& $venvStagingPython -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 13) else 13)"
if ($LASTEXITCODE -ne 0) { throw 'virtual environment is not Python 3.13' }
& $venvStagingPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip bootstrap failed with exit code $LASTEXITCODE" }
& $venvStagingPython -m pip install -r (Join-Path $ProjectRoot 'requirements-windows.txt')
if ($LASTEXITCODE -ne 0) { throw "Windows dependency install failed with exit code $LASTEXITCODE" }

$spoolProbe = Join-Path $ProjectRoot 'scripts\windows_spool_preflight.py'
& $venvStagingPython $spoolProbe --spool-dir $SpoolDir
if ($LASTEXITCODE -ne 0) {
    throw "spool capability probe failed with exit code $LASTEXITCODE; use a local NTFS directory with hard-link and shared byte-lock support"
}

$existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
$hadPreviousTask = $null -ne $existingTask
$previousTaskXml = if ($hadPreviousTask) {
    Export-ScheduledTask -TaskName $taskName
} else {
    $null
}
$taskWasRunning = $null -ne $existingTask -and $existingTask.State -eq 'Running'
if ($null -ne $existingTask -and $existingTask.State -eq 'Running') {
    Stop-ScheduledTask -TaskName $taskName
    $deadline = (Get-Date).AddSeconds(15)
    do {
        Start-Sleep -Milliseconds 200
        $existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    } while ($null -ne $existingTask -and $existingTask.State -eq 'Running' -and (Get-Date) -lt $deadline)
    if ($null -ne $existingTask -and $existingTask.State -eq 'Running') {
        throw 'owned Windows client task did not stop for upgrade'
    }
}

$hadPreviousVenv = Test-Path -LiteralPath $venvDir -PathType Container
$previousVenvMoved = $false
$venvSwapped = $false
$hookRegistrationAttempted = $false
try {
    if ($hadPreviousVenv) {
        Move-Item -LiteralPath $venvDir -Destination $venvRollbackDir
        $previousVenvMoved = $true
    }
    Move-Item -LiteralPath $venvStagingDir -Destination $venvDir
    $venvSwapped = $true
    $venvPython = Join-Path $venvDir 'Scripts\python.exe'

    $env:WORKBUDDY_CONFIG_DIR = $ConfigDir
    $env:COPILOT_STUDENT_ID = $StudentId
    $env:COPILOT_SPOOL_DIR = $SpoolDir
    $env:COPILOT_HOOK_COMMAND = $GitBashHookCommand
    $env:COPILOT_WINDOWS_WORKBUDDY_PROFILE = $ProfilePath
    $env:COPILOT_ENTRY_OWNER = $OwnerId
    $hookRegistrationAttempted = $true
    & $venvPython (Join-Path $ProjectRoot 'register_hook.py')
    if ($LASTEXITCODE -ne 0) { throw "hook registration failed with exit code $LASTEXITCODE" }
    $settingsAfterSha256 = Get-Sha256 $settingsPath

    $startScript = Join-Path $ProjectRoot 'start_windows_client.py'
    $actionArguments = @(
        (ConvertTo-NativeArgument $startScript),
        '--config',
        (ConvertTo-NativeArgument $runtimeConfigPath)
    ) -join ' '
    $action = New-ScheduledTaskAction -Execute $venvPython -Argument $actionArguments -WorkingDirectory $ProjectRoot
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity.Name
    $principal = New-ScheduledTaskPrincipal -UserId $identity.Name -LogonType Interactive -RunLevel Limited
    $taskSettings = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $taskSettings -Force | Out-Null
    Start-ScheduledTask -TaskName $taskName

    $manifestPath = Join-Path $StateDir 'installer-manifest.json'
    $manifest = [ordered]@{
        schema_version = 1
        owner_id = $OwnerId
        install_id = $installId
        student_id = $StudentId
        instance_digest = $instanceDigest
        installed_at = (Get-Date).ToUniversalTime().ToString('o')
        project_root = $ProjectRoot
        config_dir = $ConfigDir
        workbuddy_profile = $ProfilePath
        settings_path = $settingsPath
        settings_before_sha256 = $settingsBeforeSha256
        settings_after_sha256 = $settingsAfterSha256
        baseline_backup_path = $baselineBackupPath
        baseline_backup_sha256 = $baselineBackupSha256
        venv_dir = $venvDir
        task_name = $taskName
        runtime_config_path = $runtimeConfigPath
        token_file = $runtimeTokenPath
        state_dir = $StateDir
        spool_dir = $SpoolDir
        log_dir = $LogDir
    }
    Write-JsonAtomically $manifestPath $manifest
    Protect-PrivatePath $manifestPath
    Assert-PrivateAcl $manifestPath

    if (Test-Path -LiteralPath $venvRollbackDir -PathType Container) {
        Remove-Item -LiteralPath $venvRollbackDir -Recurse -Force
    }
    if (Test-Path -LiteralPath $settingsRollbackPath -PathType Leaf) {
        Remove-Item -LiteralPath $settingsRollbackPath -Force
    }
    Write-Output "Installer manifest: $manifestPath"
    Write-Output 'Windows implementation candidate installed. W1 evidence is still required for rollout.'
} catch {
    $installError = $_
    $failedTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($null -ne $failedTask -and $failedTask.State -eq 'Running') {
        Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        $rollbackDeadline = (Get-Date).AddSeconds(15)
        do {
            Start-Sleep -Milliseconds 200
            $failedTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        } while ($null -ne $failedTask -and $failedTask.State -eq 'Running' -and (Get-Date) -lt $rollbackDeadline)
    }
    if ($venvSwapped -or $previousVenvMoved) {
        Restore-PreviousVenv $venvDir $venvRollbackDir $hadPreviousVenv
    }
    if ($hookRegistrationAttempted) {
        Restore-SettingsAtomically $settingsRollbackPath $settingsPath
    }
    $failedTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($null -ne $failedTask) {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    }
    if ($hadPreviousTask) {
        Register-ScheduledTask -TaskName $taskName -Xml $previousTaskXml -Force | Out-Null
        if ($taskWasRunning -and (Test-Path -LiteralPath $venvDir -PathType Container)) {
            Start-ScheduledTask -TaskName $taskName
        }
    }
    if (Test-Path -LiteralPath $settingsRollbackPath -PathType Leaf) {
        Remove-Item -LiteralPath $settingsRollbackPath -Force
    }
    throw $installError
} finally {
    if (Test-Path -LiteralPath $venvStagingDir -PathType Container) {
        Remove-Item -LiteralPath $venvStagingDir -Recurse -Force
    }
}
