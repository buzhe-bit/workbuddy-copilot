# Read-only WorkBuddy W0 discovery.  It emits redacted metadata only and never
# changes WorkBuddy, settings.json, registry values, or local transcript data.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string]$BuildId,
    [Parameter(Mandatory = $true)] [string]$CommitSha,
    [string]$ProfilePath = ''
)

$ErrorActionPreference = 'SilentlyContinue'

function Redact([string]$Value) {
    if (-not $Value) { return $Value }
    if ($env:USERPROFILE) { $Value = $Value.Replace($env:USERPROFILE, '%USERPROFILE%') }
    $Value = $Value -replace '(?i)([A-Z]:\\ProgramData\\WorkBuddy\\users\\)[^\\]+', '$1<HASH>'
    return $Value
}

function Existing-Directory([string]$Path) {
    return $Path -and (Test-Path -LiteralPath $Path -PathType Container)
}

function Cwd-Shape([string]$Value) {
    if (-not $Value) { return $null }
    $segments = @($Value -split '[\\/]' | Where-Object { $_ }).Count
    $kind = if ($Value -match '^\\\\') {
        'unc'
    } elseif ($Value -match '^[A-Za-z]:[\\/]') {
        'drive_absolute'
    } elseif ($Value -match '^/') {
        'posix_absolute'
    } else {
        'relative_or_unknown'
    }
    $separator = if ($Value.Contains('\')) {
        'backslash'
    } elseif ($Value.Contains('/')) {
        'slash'
    } else {
        'none'
    }
    return [ordered]@{
        kind = $kind
        separator = $separator
        segment_count = $segments
        has_whitespace = [bool]($Value -match '\s')
    }
}

$out = [ordered]@{}
$out.timestamp = (Get-Date).ToUniversalTime().ToString('o')
$out.evidence = [ordered]@{
    build_id = $BuildId
    commit_sha = $CommitSha
    architecture = $env:PROCESSOR_ARCHITECTURE
}
$out.os = [ordered]@{
    version = [Environment]::OSVersion.Version.ToString()
    powershell = $PSVersionTable.PSVersion.ToString()
    userprofile = Redact $env:USERPROFILE
    workbuddy_config_env = Redact $env:WORKBUDDY_CONFIG_DIR
}

# Installation discovery reads process/registry metadata rather than assuming
# an install folder.  No executable is launched.
$out.running_processes = @(Get-Process WorkBuddy -ErrorAction SilentlyContinue | ForEach-Object {
    [ordered]@{ version = $_.FileVersion; path = Redact $_.Path }
})
$uninstallRoots = @(
    'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*',
    'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*',
    'HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*'
)
$out.installs = @($uninstallRoots | ForEach-Object { Get-ItemProperty $_ } |
    Where-Object { $_.DisplayName -match 'WorkBuddy' } |
    ForEach-Object { [ordered]@{ name = $_.DisplayName; version = $_.DisplayVersion; location = Redact $_.InstallLocation } })

# Candidate config roots are restricted to documented sources.  Paths are
# reported only after Redact; settings values and transcript content stay local.
$roots = @()
if ($env:WORKBUDDY_CONFIG_DIR) { $roots += $env:WORKBUDDY_CONFIG_DIR }
if ($env:USERPROFILE) { $roots += (Join-Path $env:USERPROFILE '.workbuddy') }
$programDataUsers = Join-Path $env:ProgramData 'WorkBuddy\users'
if (Existing-Directory $programDataUsers) {
    $roots += @(Get-ChildItem -LiteralPath $programDataUsers -Directory | ForEach-Object {
        Join-Path $_.FullName '.workbuddy'
    })
}
if ($env:SystemDrive) {
    $workBuddyEnv = Join-Path $env:SystemDrive 'WorkBuddy-env'
    if (Existing-Directory $workBuddyEnv) {
        $roots += @(Get-ChildItem -LiteralPath $workBuddyEnv -Directory | ForEach-Object {
            Join-Path $_.FullName '.workbuddy'
        })
    }
}
$roots = @($roots | Where-Object { Existing-Directory $_ } | Select-Object -Unique)

$out.config_roots = @($roots | ForEach-Object {
    $root = $_
    $record = [ordered]@{
        path = Redact $root
        top = @(Get-ChildItem -LiteralPath $root -Force | ForEach-Object {
            [ordered]@{
                name = $_.Name
                kind = if ($_.PSIsContainer) { 'dir' } else { 'file' }
                bytes = if ($_.PSIsContainer) { $null } else { $_.Length }
                mtime = $_.LastWriteTimeUtc.ToString('o')
            }
        })
    }

    $settings = Join-Path $root 'settings.json'
    if (Test-Path -LiteralPath $settings -PathType Leaf) {
        try {
            $settingsObject = Get-Content -LiteralPath $settings -Raw -Encoding UTF8 | ConvertFrom-Json
            $record.settings = [ordered]@{
                top_keys = @($settingsObject.PSObject.Properties.Name)
                hook_events = @($settingsObject.hooks.PSObject.Properties.Name)
                hook_types = @($settingsObject.hooks.PSObject.Properties | ForEach-Object {
                    @($_.Value.hooks.type) | Select-Object -Unique
                } | Select-Object -Unique)
            }
        } catch {
            $record.settings_error = $_.Exception.GetType().Name
        }
    }

    $database = Join-Path $root 'workbuddy.db'
    $record.db_files = @('workbuddy.db', 'workbuddy.db-wal', 'workbuddy.db-shm' | ForEach-Object {
        $candidate = Join-Path $root $_
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            $item = Get-Item -LiteralPath $candidate
            [ordered]@{ name = $item.Name; bytes = $item.Length; mtime = $item.LastWriteTimeUtc.ToString('o') }
        }
    })
    $sqlite = Get-Command sqlite3 -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($sqlite -and (Test-Path -LiteralPath $database -PathType Leaf)) {
        $record.sqlite = [ordered]@{
            tables = @(& $sqlite.Source -readonly $database '.tables')
            sessions_columns = @(& $sqlite.Source -readonly $database 'PRAGMA table_info(sessions);')
            workspaces_columns = @(& $sqlite.Source -readonly $database 'PRAGMA table_info(workspaces);')
            journal_mode = @(& $sqlite.Source -readonly $database 'PRAGMA journal_mode;')
        }
    } else {
        $record.sqlite = 'sqlite3_missing_or_db_missing'
    }

    $projects = Join-Path $root 'projects'
    if (Existing-Directory $projects) {
        $files = @(Get-ChildItem -LiteralPath $projects -Filter '*.jsonl' -File -Recurse |
            Sort-Object LastWriteTimeUtc -Descending)
        $record.transcripts = [ordered]@{
            count = $files.Count
            samples = @($files | Select-Object -First 5 | ForEach-Object {
                [ordered]@{ project_dir = $_.Directory.Name; file = $_.Name; bytes = $_.Length; mtime = $_.LastWriteTimeUtc.ToString('o') }
            })
        }
        $sample = $files | Select-Object -First 1
        if ($sample) {
            $types = @()
            $keys = @()
            $cwdShape = $null
            Get-Content -LiteralPath $sample.FullName -Encoding UTF8 -TotalCount 30 | ForEach-Object {
                try {
                    $line = $_ | ConvertFrom-Json
                    $types += $line.type
                    $keys += $line.PSObject.Properties.Name
                    if (-not $cwdShape -and $line.cwd) { $cwdShape = Cwd-Shape ([string]$line.cwd) }
                } catch {}
            }
            $record.transcripts.first30_metadata = [ordered]@{
                types = @($types | Where-Object { $_ } | Select-Object -Unique)
                keys = @($keys | Select-Object -Unique)
                cwd_shape = $cwdShape
            }
        }
    }
    $record
})

$out.commands = @('python', 'py', 'python3', 'bash', 'git', 'sqlite3' | ForEach-Object {
    $command = Get-Command $_ -ErrorAction SilentlyContinue | Select-Object -First 1
    [ordered]@{ name = $_; found = [bool]$command; type = $command.CommandType; path = Redact $command.Source }
})
$runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$out.hkcu_run = @(Get-ItemProperty $runKey | Select-Object -ExpandProperty PSObject | Select-Object -ExpandProperty Properties |
    Where-Object { $_.Name -match 'WorkBuddy' } |
    ForEach-Object { [ordered]@{ name = $_.Name; value = Redact ([string]$_.Value) } })
if (Get-Command Get-ScheduledTask -ErrorAction SilentlyContinue) {
    $out.scheduled_tasks = @(Get-ScheduledTask | Where-Object { $_.TaskName -match 'WorkBuddy' -or $_.TaskPath -match 'WorkBuddy' } |
        ForEach-Object { [ordered]@{ name = $_.TaskName; path = $_.TaskPath; state = $_.State.ToString() } })
}

$blockedReasons = @()
if ($out.running_processes.Count -eq 0 -and $out.installs.Count -eq 0) {
    $blockedReasons += 'workbuddy_not_detected'
}
if ($out.config_roots.Count -eq 0) { $blockedReasons += 'config_not_detected' }
if (-not $ProfilePath -or -not (Test-Path -LiteralPath $ProfilePath -PathType Leaf)) {
    $blockedReasons += 'profile_missing'
}
$gate = [ordered]@{
    status = 'passed'
    blocked_reasons = $blockedReasons
    verified_profile = Redact $ProfilePath
}
if ($blockedReasons.Count -gt 0) { $gate.status = 'blocked' }
$out.gate = $gate

$out | ConvertTo-Json -Depth 12
