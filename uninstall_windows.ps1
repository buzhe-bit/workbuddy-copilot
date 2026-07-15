# Manifest-scoped WorkBuddy Copilot Windows uninstaller.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string]$ManifestPath
)

$ErrorActionPreference = 'Stop'
$ExpectedOwnerId = 'workbuddy-copilot-v1'
$ExpectedProjectRoot = [System.IO.Path]::GetFullPath(
    (Split-Path -LiteralPath $PSCommandPath -Parent)
).TrimEnd('\', '/')

if (-not [System.IO.Path]::IsPathFullyQualified($ManifestPath)) {
    throw 'ManifestPath must be an absolute path'
}

function Get-CurrentUserSid {
    return [System.Security.Principal.WindowsIdentity]::GetCurrent().User
}

function Protect-PrivatePath([string]$Path) {
    $item = Get-Item -LiteralPath $Path -Force
    $sid = Get-CurrentUserSid
    $acl = Get-Acl -LiteralPath $item.FullName
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($rule in @($acl.Access)) { [void]$acl.RemoveAccessRuleAll($rule) }
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
    if (-not (Test-Path -LiteralPath $Path)) { throw "private ACL path is missing: $Path" }
    $sid = Get-CurrentUserSid
    $acl = Get-Acl -LiteralPath $Path
    if (-not $acl.AreAccessRulesProtected) { throw "private ACL inheritance is enabled: $Path" }
    try {
        $ownerSid = (New-Object System.Security.Principal.NTAccount($acl.Owner)).Translate(
            [System.Security.Principal.SecurityIdentifier]
        )
    } catch {
        $ownerSid = New-Object System.Security.Principal.SecurityIdentifier($acl.Owner)
    }
    if ($ownerSid.Value -ne $sid.Value) { throw "private ACL owner mismatch: $Path" }
    $hasCurrentUserAllow = $false
    $rules = $acl.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier])
    foreach ($rule in $rules) {
        if ($rule.IsInherited) { throw "private ACL contains inherited entries: $Path" }
        if ($rule.AccessControlType -eq [System.Security.AccessControl.AccessControlType]::Allow) {
            if ($rule.IdentityReference.Value -ne $sid.Value) {
                throw "private ACL grants another identity: $Path"
            }
            $hasCurrentUserAllow = $true
        }
    }
    if (-not $hasCurrentUserAllow) { throw "private ACL does not grant current user: $Path" }
}

function Get-Sha256([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-InstanceDigest([string]$StudentId) {
    if ([string]::IsNullOrWhiteSpace($StudentId)) {
        throw 'installer manifest student_id is missing'
    }
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        return [System.BitConverter]::ToString(
            $sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($StudentId))
        ).Replace('-', '').Substring(0, 16).ToLowerInvariant()
    } finally {
        $sha.Dispose()
    }
}

function Assert-PathEquals([string]$Actual, [string]$Expected, [string]$Name) {
    $actualFull = [System.IO.Path]::GetFullPath($Actual).TrimEnd('\', '/')
    $expectedFull = [System.IO.Path]::GetFullPath($Expected).TrimEnd('\', '/')
    if (-not $actualFull.Equals($expectedFull, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "installer manifest $Name is not an owned path"
    }
}

function Assert-PathInside([string]$Child, [string]$Parent) {
    $separator = [System.IO.Path]::DirectorySeparatorChar
    $parentFull = [System.IO.Path]::GetFullPath($Parent).TrimEnd('\', '/')
    $childFull = [System.IO.Path]::GetFullPath($Child).TrimEnd('\', '/')
    $prefix = $parentFull + $separator
    if ($childFull -ne $parentFull -and
        -not $childFull.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "installer manifest path escapes owned directory: $Child"
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

function Write-SettingsBytesAtomically([string]$Path, [byte[]]$Bytes) {
    $directory = Split-Path -LiteralPath $Path -Parent
    $temporary = Join-Path $directory ('.{0}.{1}.tmp' -f (Split-Path -Leaf $Path), [Guid]::NewGuid().ToString('N'))
    try {
        [System.IO.File]::WriteAllBytes($temporary, $Bytes)
        if (Test-Path -LiteralPath $Path -PathType Leaf) {
            [System.IO.File]::Replace($temporary, $Path, $null, $true)
        } else {
            Move-Item -LiteralPath $temporary -Destination $Path
        }
    } finally {
        if (Test-Path -LiteralPath $temporary -PathType Leaf) {
            Remove-Item -LiteralPath $temporary -Force
        }
    }
}

function Write-SettingsAtomically([string]$Path, [object]$Value) {
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    $json = ($Value | ConvertTo-Json -Depth 20) + [Environment]::NewLine
    Write-SettingsBytesAtomically $Path ($utf8.GetBytes($json))
}

function Restore-SettingsAtomically([string]$BackupPath, [string]$SettingsPath) {
    Write-SettingsBytesAtomically $SettingsPath ([System.IO.File]::ReadAllBytes($BackupPath))
}

function Remove-OwnedHookEntries([object]$Settings, [string]$OwnerId) {
    if ($null -eq $Settings.hooks) { return $Settings }
    foreach ($eventProperty in @($Settings.hooks.PSObject.Properties)) {
        $retainedBlocks = New-Object System.Collections.ArrayList
        foreach ($block in @($eventProperty.Value)) {
            $retainedHooks = New-Object System.Collections.ArrayList
            foreach ($hook in @($block.hooks)) {
                $command = [string]$hook.command
                if (-not $command.Contains("COPILOT_ENTRY_OWNER=$OwnerId")) {
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

if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
    throw "installer-manifest.json is missing: $ManifestPath"
}
Assert-PrivateAcl $ManifestPath
$manifest = Get-Content -LiteralPath $ManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ([string]$manifest.owner_id -ne $ExpectedOwnerId) {
    throw 'installer manifest owner mismatch'
}
if ([int]$manifest.schema_version -ne 1) {
    throw 'installer manifest schema mismatch'
}

$stateDir = [System.IO.Path]::GetFullPath([string]$manifest.state_dir)
$manifestFullPath = [System.IO.Path]::GetFullPath($ManifestPath)
Assert-PathEquals $manifestFullPath (Join-Path $stateDir 'installer-manifest.json') 'manifest path'
Assert-NoReparsePointChain $stateDir
Assert-NoReparsePointChain $manifestFullPath

$projectRoot = [System.IO.Path]::GetFullPath([string]$manifest.project_root)
Assert-PathEquals $projectRoot $ExpectedProjectRoot 'project_root'
Assert-NoReparsePointChain $projectRoot

$settingsPath = [System.IO.Path]::GetFullPath([string]$manifest.settings_path)
$configDir = [System.IO.Path]::GetFullPath([string]$manifest.config_dir)
Assert-PathInside $settingsPath $configDir
Assert-PathEquals $settingsPath (Join-Path $configDir 'settings.json') 'settings_path'
Assert-NoReparsePointChain $configDir
Assert-NoReparsePointChain $settingsPath
if (-not (Test-Path -LiteralPath $settingsPath -PathType Leaf)) {
    throw 'WorkBuddy settings file is missing during uninstall'
}
$currentSettingsHash = Get-Sha256 $settingsPath
$installedSettingsHash = ([string]$manifest.settings_after_sha256).ToLowerInvariant()
if ($installedSettingsHash -notmatch '^[0-9a-f]{64}$') {
    throw 'installer manifest settings hash is invalid'
}
$baselineBackupPath = [System.IO.Path]::GetFullPath([string]$manifest.baseline_backup_path)
Assert-PathInside $baselineBackupPath $stateDir
$installId = [string]$manifest.install_id
if ($installId -notmatch '^[0-9a-f]{32}$') {
    throw 'installer manifest install_id is invalid'
}
Assert-PathEquals $baselineBackupPath (Join-Path $stateDir "settings-baseline-$installId.json") 'baseline_backup_path'
Assert-NoReparsePointChain $baselineBackupPath
$baselineHash = ([string]$manifest.baseline_backup_sha256).ToLowerInvariant()
if ($baselineHash -notmatch '^[0-9a-f]{64}$') {
    throw 'installer manifest baseline hash is invalid'
}
if (-not (Test-Path -LiteralPath $baselineBackupPath -PathType Leaf)) {
    throw 'baseline settings backup is missing'
}
Assert-PrivateAcl $baselineBackupPath
if ((Get-Sha256 $baselineBackupPath) -ne $baselineHash) {
    throw 'baseline settings backup hash mismatch'
}

$expectedDigest = Get-InstanceDigest ([string]$manifest.student_id)
if ([string]$manifest.instance_digest -ne $expectedDigest) {
    throw 'installer manifest instance digest mismatch'
}
$expectedTaskName = "WorkBuddyCopilot-$expectedDigest"
$taskName = [string]$manifest.task_name
if ($taskName -ne $expectedTaskName) {
    throw 'installer manifest task name is not owned by this student instance'
}

$expectedVenv = [System.IO.Path]::GetFullPath((Join-Path $projectRoot '.venv-win'))
$manifestVenv = [System.IO.Path]::GetFullPath([string]$manifest.venv_dir)
Assert-PathEquals $manifestVenv $expectedVenv 'venv_dir'
Assert-NoReparsePointChain $manifestVenv

$runtimeConfigPath = [System.IO.Path]::GetFullPath([string]$manifest.runtime_config_path)
$tokenFile = [System.IO.Path]::GetFullPath([string]$manifest.token_file)
$spoolDir = [System.IO.Path]::GetFullPath([string]$manifest.spool_dir)
$logDir = [System.IO.Path]::GetFullPath([string]$manifest.log_dir)
Assert-PathInside $runtimeConfigPath $stateDir
Assert-PathInside $tokenFile $stateDir
Assert-PathInside $spoolDir $stateDir
Assert-PathInside $logDir $stateDir
Assert-PathEquals $runtimeConfigPath (Join-Path $stateDir 'client-config.json') 'runtime_config_path'
Assert-PathEquals $tokenFile (Join-Path $stateDir 'student.token') 'token_file'
Assert-NoReparsePointChain $runtimeConfigPath
Assert-NoReparsePointChain $tokenFile
Assert-NoReparsePointChain $spoolDir
Assert-NoReparsePointChain $logDir

# All manifest data and owned paths are validated before the first mutation.
$task = Get-ScheduledTask -TaskName $expectedTaskName -ErrorAction SilentlyContinue
if ($null -ne $task -and $task.State -eq 'Running') {
    Stop-ScheduledTask -TaskName $expectedTaskName
    $deadline = (Get-Date).AddSeconds(15)
    do {
        Start-Sleep -Milliseconds 200
        $task = Get-ScheduledTask -TaskName $expectedTaskName -ErrorAction SilentlyContinue
    } while ($null -ne $task -and $task.State -eq 'Running' -and (Get-Date) -lt $deadline)
    if ($null -ne $task -and $task.State -eq 'Running') {
        throw 'owned Windows client task did not stop during uninstall'
    }
}

if ($currentSettingsHash -eq $installedSettingsHash) {
    Restore-SettingsAtomically $baselineBackupPath $settingsPath
} else {
    Write-Output 'settings hash changed; removing only the owned Copilot hook entries'
    $settings = Get-Content -LiteralPath $settingsPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $settings = Remove-OwnedHookEntries $settings $ExpectedOwnerId
    Write-SettingsAtomically $settingsPath $settings
}

if ($null -ne $task) {
    Unregister-ScheduledTask -TaskName $expectedTaskName -Confirm:$false
}

if (Test-Path -LiteralPath $manifestVenv -PathType Container) {
    Remove-Item -LiteralPath $manifestVenv -Recurse -Force
}

$manifest | Add-Member -NotePropertyName uninstalled_at -NotePropertyValue ((Get-Date).ToUniversalTime().ToString('o')) -Force
$utf8 = New-Object System.Text.UTF8Encoding($false)
$manifestBytes = $utf8.GetBytes((($manifest | ConvertTo-Json -Depth 20) + [Environment]::NewLine))
$manifestTemporary = Join-Path (Split-Path -LiteralPath $ManifestPath -Parent) ('.installer-manifest.{0}.tmp' -f [Guid]::NewGuid().ToString('N'))
try {
    [System.IO.File]::WriteAllBytes($manifestTemporary, $manifestBytes)
    Protect-PrivatePath $manifestTemporary
    Assert-PrivateAcl $manifestTemporary
    [System.IO.File]::Replace($manifestTemporary, $ManifestPath, $null, $true)
    Assert-PrivateAcl $ManifestPath
} finally {
    if (Test-Path -LiteralPath $manifestTemporary -PathType Leaf) {
        Remove-Item -LiteralPath $manifestTemporary -Force
    }
}

Write-Output 'WorkBuddy Copilot owned hook/task/venv removed; student state retained.'
