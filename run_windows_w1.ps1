# Real-machine Windows W1 evidence runner. This script is intentionally not
# used as a hosted-CI release gate: a trusted self-hosted runner and all manual
# hardware gates are required.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string]$ProjectRoot,
    [Parameter(Mandatory = $true)] [string]$ExpectedCommit,
    [Parameter(Mandatory = $true)] [string]$BuildId,
    [Parameter(Mandatory = $true)] [string]$RunnerId,
    [Parameter(Mandatory = $true)] [string]$WorkBuddyVersion,
    [Parameter(Mandatory = $true)] [string]$WorkBuddyBuild,
    [Parameter(Mandatory = $true)] [string]$InstallerManifestPath,
    [Parameter(Mandatory = $true)] [string]$LifecycleLogPath,
    [Parameter(Mandatory = $true)] [string]$GateResultsPath,
    [Parameter(Mandatory = $true)] [string]$GitBashPath,
    [Parameter(Mandatory = $true)] [string]$ChineseTestRoot,
    [Parameter(Mandatory = $true)] [string]$IdentityCheckUrl,
    [Parameter(Mandatory = $true)] [string]$StudentATokenFile,
    [Parameter(Mandatory = $true)] [string]$RecoveryResultPath,
    [string]$OutputDirectory = ''
)

$ErrorActionPreference = 'Stop'
$RequiredGateIds = @(
    'install_upgrade_uninstall',
    'workbuddy_git_bash_hook',
    'native_floating_ui',
    'dpi_multimonitor',
    'focus_drag_topmost',
    'chinese_user_path',
    'sleep_wake',
    'login_autostart',
    'disconnect_recovery',
    'identity_isolation',
    'antivirus_compatibility'
)

function Write-Utf8NoBom([string]$Path, [string]$Text) {
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Text, $encoding)
}

function Require-File([string]$Path, [string]$Name) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Name is missing: $Path"
    }
}

function Copy-EvidenceArtifact([string]$Source, [string]$Destination) {
    Require-File $Source 'evidence artifact'
    $sourceFull = [System.IO.Path]::GetFullPath($Source)
    $destinationFull = [System.IO.Path]::GetFullPath($Destination)
    if (-not $sourceFull.Equals($destinationFull, [System.StringComparison]::OrdinalIgnoreCase)) {
        Copy-Item -LiteralPath $sourceFull -Destination $destinationFull -Force
    }
    return [ordered]@{
        path = [System.IO.Path]::GetFileName($destinationFull)
        sha256 = (Get-FileHash -LiteralPath $destinationFull -Algorithm SHA256).Hash.ToLowerInvariant()
    }
}

if ($env:GITHUB_ACTIONS -eq 'true' -and $env:RUNNER_ENVIRONMENT -ne 'self-hosted') {
    Write-Output 'implementation_candidate: hosted Windows is not trusted W1 evidence'
    exit 2
}
if ($ExpectedCommit -notmatch '^[0-9a-f]{40}$') {
    throw 'ExpectedCommit must be a lowercase 40-character Git commit'
}
if ([string]::IsNullOrWhiteSpace($BuildId) -or [string]::IsNullOrWhiteSpace($RunnerId)) {
    throw 'BuildId and RunnerId are required'
}

$ProjectRoot = [System.IO.Path]::GetFullPath($ProjectRoot)
Require-File (Join-Path $ProjectRoot 'scripts\validate_windows_evidence.py') 'validator'
Require-File $InstallerManifestPath 'installer manifest'
Require-File $LifecycleLogPath 'lifecycle log'
Require-File $GateResultsPath 'manual gate results'
Require-File $GitBashPath 'Git Bash executable'
Require-File $StudentATokenFile 'student A token file'
Require-File $RecoveryResultPath 'disconnect recovery result'
if (-not (Test-Path -LiteralPath $ChineseTestRoot -PathType Container)) {
    throw "Chinese test root is missing: $ChineseTestRoot"
}
$installerManifest = Get-Content -LiteralPath $InstallerManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
$verifiedProfilePath = [string]$installerManifest.workbuddy_profile
Require-File $verifiedProfilePath 'verified WorkBuddy profile'

$currentCommit = (& git -C $ProjectRoot rev-parse HEAD).Trim().ToLowerInvariant()
if ($LASTEXITCODE -ne 0 -or $currentCommit -ne $ExpectedCommit) {
    throw "git rev-parse HEAD does not match ExpectedCommit"
}
$worktreeStatus = @(& git -C $ProjectRoot status --porcelain --untracked-files=normal)
if ($LASTEXITCODE -ne 0) {
    throw 'git status failed while validating the W1 source tree'
}
if (@($worktreeStatus | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }).Count -ne 0) {
    throw 'W1 requires a clean source tree with no tracked or untracked changes'
}

$pythonVersion = (& py -3.13 -c 'import platform; print(platform.python_version())').Trim()
if ($LASTEXITCODE -ne 0 -or $pythonVersion -notmatch '^3\.13\.[0-9]+$') {
    throw 'W1 requires Python 3.13.x selected through py -3.13'
}

if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $OutputDirectory = Join-Path $ProjectRoot 'windows-w1-evidence'
}
$OutputDirectory = [System.IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null

$w0Path = Join-Path $OutputDirectory 'w0-probe.json'
$probeOutput = & (Join-Path $ProjectRoot 'probe_windows_workbuddy.ps1') `
    -BuildId $BuildId `
    -CommitSha $currentCommit `
    -ProfilePath $verifiedProfilePath
if ($LASTEXITCODE -ne 0) { throw 'W0 WorkBuddy probe failed' }
$probePayload = ($probeOutput | Out-String) | ConvertFrom-Json
if ($probePayload.gate.status -ne 'passed') {
    throw "W0 WorkBuddy probe blocked: $($probePayload.gate.blocked_reasons -join ',')"
}
Write-Utf8NoBom $w0Path (($probeOutput | Out-String).Trim() + [Environment]::NewLine)

$pytestPath = Join-Path $OutputDirectory 'w1-pytest.txt'
$junitPath = Join-Path $OutputDirectory 'w1-pytest.xml'
$env:WORKBUDDY_W1_INSTALLER_MANIFEST = $InstallerManifestPath
$env:WORKBUDDY_W1_LIFECYCLE_LOG = $LifecycleLogPath
$env:WORKBUDDY_W1_GIT_BASH = $GitBashPath
$env:WORKBUDDY_W1_CHINESE_TEST_ROOT = $ChineseTestRoot
$env:WORKBUDDY_W1_IDENTITY_CHECK_URL = $IdentityCheckUrl
$env:WORKBUDDY_W1_STUDENT_A_TOKEN_FILE = $StudentATokenFile
$env:WORKBUDDY_W1_RECOVERY_RESULT = $RecoveryResultPath
$pytestOutput = & py -3.13 -m pytest (Join-Path $ProjectRoot 'tests\test_windows_w1_real_machine.py') -m 'windows and real_machine' `
    --junitxml $junitPath -q 2>&1
$pytestStatus = $LASTEXITCODE
Write-Utf8NoBom $pytestPath (($pytestOutput | Out-String).TrimEnd() + [Environment]::NewLine)
if ($pytestStatus -ne 0) {
    Write-Output 'implementation_candidate: real-machine pytest gate failed'
    exit $pytestStatus
}
[xml]$junit = Get-Content -LiteralPath $junitPath -Raw -Encoding UTF8
$executedTestCount = @($junit.testsuites.testsuite | ForEach-Object { [int]$_.tests } |
    Measure-Object -Sum).Sum
if ($null -eq $executedTestCount -or $executedTestCount -le 0) {
    throw 'real-machine pytest gate executed zero tests'
}
$critical_skip_count = @($junit.testsuites.testsuite | ForEach-Object { [int]$_.skipped } |
    Measure-Object -Sum).Sum
if ($null -eq $critical_skip_count) { $critical_skip_count = 0 }
if ($critical_skip_count -ne 0) {
    throw "critical_skip_count must be zero; found $critical_skip_count"
}

$gatePayload = Get-Content -LiteralPath $GateResultsPath -Raw -Encoding UTF8 | ConvertFrom-Json
$gateRows = @($gatePayload.tests)
foreach ($gateId in $RequiredGateIds) {
    $matching = @($gateRows | Where-Object { $_.id -eq $gateId -and $_.status -eq 'passed' })
    if ($matching.Count -ne 1) {
        throw "manual W1 gate is missing, duplicated, or not passed: $gateId"
    }
}
if ($gateRows.Count -ne $RequiredGateIds.Count) {
    throw 'manual W1 gate file contains unknown or duplicate entries'
}

$manifestCopy = Join-Path $OutputDirectory 'installer-manifest.json'
$lifecycleCopy = Join-Path $OutputDirectory 'lifecycle-log.json'
$w0Artifact = Copy-EvidenceArtifact $w0Path $w0Path
$pytestArtifact = Copy-EvidenceArtifact $pytestPath $pytestPath
$manifestArtifact = Copy-EvidenceArtifact $InstallerManifestPath $manifestCopy
$lifecycleArtifact = Copy-EvidenceArtifact $LifecycleLogPath $lifecycleCopy

$machineGuid = (Get-ItemProperty -LiteralPath 'HKLM:\SOFTWARE\Microsoft\Cryptography' -Name MachineGuid).MachineGuid
if ([string]::IsNullOrWhiteSpace($machineGuid)) { throw 'Windows MachineGuid is unavailable' }
$machineBytes = [System.Text.Encoding]::UTF8.GetBytes([string]$machineGuid)
$machineSha = [System.Security.Cryptography.SHA256]::Create()
try {
    $machineIdSha256 = [System.BitConverter]::ToString($machineSha.ComputeHash($machineBytes)).Replace('-', '').ToLowerInvariant()
} finally {
    $machineSha.Dispose()
}

$evidencePath = Join-Path $OutputDirectory 'windows-w1-evidence.json'
$evidence = [ordered]@{
    schema_version = 1
    evidence_level = 'W1'
    commit_sha = $currentCommit
    build_id = $BuildId
    generated_at = (Get-Date).ToUniversalTime().ToString('o')
    execution_environment = [ordered]@{
        kind = 'self_hosted_windows'
        runner_id = $RunnerId
        machine_id_sha256 = $machineIdSha256
    }
    platform = [ordered]@{
        os = 'Windows'
        version = [Environment]::OSVersion.Version.ToString()
        architecture = $env:PROCESSOR_ARCHITECTURE
    }
    python_version = $pythonVersion
    workbuddy = [ordered]@{ version = $WorkBuddyVersion; build = $WorkBuddyBuild }
    critical_skip_count = $critical_skip_count
    tests = @($gateRows | ForEach-Object { [ordered]@{ id = [string]$_.id; status = 'passed' } })
    artifacts = @(
        [ordered]@{ id = 'w0_probe'; path = $w0Artifact.path; sha256 = $w0Artifact.sha256 },
        [ordered]@{ id = 'w1_pytest'; path = $pytestArtifact.path; sha256 = $pytestArtifact.sha256 },
        [ordered]@{ id = 'installer_manifest'; path = $manifestArtifact.path; sha256 = $manifestArtifact.sha256 },
        [ordered]@{ id = 'lifecycle_log'; path = $lifecycleArtifact.path; sha256 = $lifecycleArtifact.sha256 }
    )
    evidence_sha256 = ('0' * 64)
}
Write-Utf8NoBom $evidencePath (($evidence | ConvertTo-Json -Depth 20) + [Environment]::NewLine)

$validator = Join-Path $ProjectRoot 'scripts\validate_windows_evidence.py'
$schema = Join-Path $ProjectRoot 'copilot\student_platform\windows_evidence.schema.json'
& py -3.13 $validator `
    --evidence $evidencePath `
    --schema $schema `
    --expected-commit $ExpectedCommit `
    --expected-build $BuildId `
    --expected-runner-id $RunnerId `
    --seal
if ($LASTEXITCODE -ne 0) {
    Write-Output 'implementation_candidate: evidence validation failed'
    exit $LASTEXITCODE
}
Write-Output "Validated W1 evidence: $evidencePath"
