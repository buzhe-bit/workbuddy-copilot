# Requires a W0-verified Git Bash hook command.  This installer never guesses
# WorkBuddy's config root, executable location, project dirname encoding, or
# the Python command visible inside WorkBuddy's Git Bash hook environment.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string]$ProjectRoot,
    [Parameter(Mandatory = $true)] [string]$ConfigDir,
    [Parameter(Mandatory = $true)] [string]$ProfilePath,
    [Parameter(Mandatory = $true)] [string]$StudentId,
    [Parameter(Mandatory = $true)] [string]$GitBashHookCommand,
    [Parameter(Mandatory = $true)] [string]$BaseUrl,
    [string]$PythonCommand = 'py',
    [string]$SpoolDir = '',
    [string]$StateDir = ''
)

$ErrorActionPreference = 'Stop'

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

function ConvertTo-NativeArgument([string]$Value) {
    # Start-Process joins ArgumentList entries into one native command line.
    # Quote whitespace and apply the CommandLineToArgvW backslash rules so
    # Chinese/space-bearing WorkBuddy paths remain one argument.
    if ($Value -notmatch '[\s"]') {
        return $Value
    }
    $escaped = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
    $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
    return '"' + $escaped + '"'
}

function Backup-SettingsAtomically([string]$SettingsPath) {
    $directory = Split-Path -LiteralPath $SettingsPath -Parent
    $backupPath = Join-Path $directory ("settings.json.copilot-backup.{0}.json" -f (Get-Date -Format 'yyyyMMddHHmmss'))
    $temporaryBackup = Join-Path $directory (".settings.json.copilot-backup.{0}.tmp" -f [Guid]::NewGuid().ToString('N'))
    try {
        [System.IO.File]::WriteAllBytes($temporaryBackup, [System.IO.File]::ReadAllBytes($SettingsPath))
        Move-Item -LiteralPath $temporaryBackup -Destination $backupPath
    } finally {
        if (Test-Path -LiteralPath $temporaryBackup -PathType Leaf) {
            Remove-Item -LiteralPath $temporaryBackup -Force
        }
    }
    return $backupPath
}

Require-Directory $ProjectRoot 'ProjectRoot'
Require-Directory $ConfigDir 'ConfigDir'
Require-File $ProfilePath 'ProfilePath'
$settingsPath = Join-Path $ConfigDir 'settings.json'
if (-not (Test-Path -LiteralPath $settingsPath -PathType Leaf)) {
    throw "settings.json is missing from the explicit ConfigDir: $ConfigDir"
}
if ([string]::IsNullOrWhiteSpace($GitBashHookCommand)) {
    throw 'Installation requires W0-verified Git Bash hook command; no command was constructed.'
}

$venvDir = Join-Path $ProjectRoot '.venv-win'
& $PythonCommand -3 -m venv $venvDir
if ($LASTEXITCODE -ne 0) { throw "virtual environment creation failed with exit code $LASTEXITCODE" }
$venvPython = Join-Path $venvDir 'Scripts\python.exe'
& $venvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip bootstrap failed with exit code $LASTEXITCODE" }
& $venvPython -m pip install -r (Join-Path $ProjectRoot 'requirements-windows.txt')
if ($LASTEXITCODE -ne 0) { throw "Windows dependency install failed with exit code $LASTEXITCODE" }

if ([string]::IsNullOrWhiteSpace($SpoolDir)) {
    $SpoolDir = Join-Path $ConfigDir 'copilot\spool'
}
New-Item -ItemType Directory -Force -Path $SpoolDir | Out-Null
$spoolProbe = Join-Path $ProjectRoot 'scripts\windows_spool_preflight.py'
& $venvPython $spoolProbe --spool-dir $SpoolDir
if ($LASTEXITCODE -ne 0) {
    throw "spool capability probe failed with exit code $LASTEXITCODE; use a local NTFS directory with hard-link and shared byte-lock support"
}
if ([string]::IsNullOrWhiteSpace($StateDir)) {
    $StateDir = Join-Path $ConfigDir 'copilot\state'
}
New-Item -ItemType Directory -Force -Path $StateDir | Out-Null

# register_hook.py prefixes the explicit identity/config/spool values to the
# command body that W0 tested in WorkBuddy's actual Git Bash environment.  It
# rejects command bodies that try to override those owned variables.
$env:WORKBUDDY_CONFIG_DIR = $ConfigDir
$env:COPILOT_STUDENT_ID = $StudentId
$env:COPILOT_SPOOL_DIR = $SpoolDir
$env:COPILOT_HOOK_COMMAND = $GitBashHookCommand
$env:COPILOT_WINDOWS_WORKBUDDY_PROFILE = $ProfilePath
$backupPath = Backup-SettingsAtomically $settingsPath
& $venvPython (Join-Path $ProjectRoot 'register_hook.py')
if ($LASTEXITCODE -ne 0) { throw "hook registration failed with exit code $LASTEXITCODE" }

# The agent receives all runtime locations through explicit parameters/env, not
# via an assumed Windows home or WorkBuddy installation directory.
$agentArgs = @(
    (Join-Path $ProjectRoot 'start_student_agent.py'),
    '--platform', 'windows',
    '--base-url', $BaseUrl,
    '--student-id', $StudentId,
    '--spool-dir', $SpoolDir,
    '--state-dir', $StateDir,
    '--workbuddy-config-dir', $ConfigDir,
    '--workbuddy-profile', $ProfilePath
)
$nativeAgentArgs = @($agentArgs | ForEach-Object { ConvertTo-NativeArgument ([string]$_) })
Start-Process -FilePath $venvPython -ArgumentList $nativeAgentArgs -WorkingDirectory $ProjectRoot

Write-Output "Settings backup created: $backupPath"
Write-Output 'Hook registered with the supplied W0-verified Git Bash command. Confirm it in WorkBuddy /hooks before rollout.'
