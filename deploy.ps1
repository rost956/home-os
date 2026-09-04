param(
    [Parameter(Mandatory=$true)]
    [string]$ServerHost,

    [Parameter(Mandatory=$true)]
    [string]$ServerUser,

    [int]$ServerPort = 22,
    [string]$RemoteDir = "/opt/recipe_budget_service",
    [string]$LocalDir = (Get-Location).Path,
    [switch]$UseSudoDocker
)

$ErrorActionPreference = "Stop"

function Step([string]$Text) {
    Write-Host ""
    Write-Host ("==> " + $Text) -ForegroundColor Cyan
}

function Run-Cmd([string]$Exe, [string[]]$ArgsList) {
    & $Exe @ArgsList
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed: $Exe"
    }
}

function Quote-BashArg([AllowNull()][string]$Text) {
    if ($null -eq $Text) { $Text = "" }
    return "'" + ($Text -replace "'", "'\''") + "'"
}

$LocalDir = [System.IO.Path]::GetFullPath($LocalDir)
$RemoteDir = $RemoteDir.Trim()
if ([string]::IsNullOrWhiteSpace($RemoteDir) -or $RemoteDir -eq "/") {
    throw "RemoteDir is empty or unsafe"
}
foreach ($RequiredPath in @("app", "scripts", "ops", "Dockerfile", "docker-compose.yml", "Caddyfile", "requirements.txt")) {
    if (!(Test-Path -LiteralPath (Join-Path $LocalDir $RequiredPath))) {
        throw "$RequiredPath not found in $LocalDir"
    }
}

$Stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$TempDir = Join-Path ([System.IO.Path]::GetTempPath()) ("recipe_deploy_" + $Stamp)
$Archive = Join-Path $TempDir "source.tar.gz"
$RemoteArchive = "/tmp/recipe-budget-source-$Stamp.tar.gz"
$RemoteStage = "/tmp/recipe-budget-source-$Stamp"
$RemoteTarget = "$ServerUser@$ServerHost"

try {
    New-Item -ItemType Directory -Force -Path $TempDir | Out-Null
    Step "Packing source without local data or secrets"
    Run-Cmd "tar.exe" @(
        "-czf", $Archive,
        "-C", $LocalDir,
        "app", "scripts", "ops", "Dockerfile", "docker-compose.yml", "Caddyfile", "requirements.txt"
    )

    Step "Uploading source"
    Run-Cmd "scp" @("-P", [string]$ServerPort, $Archive, ($RemoteTarget + ":" + $RemoteArchive))

    Step "Running guarded production deployment"
    $SudoDocker = if ($UseSudoDocker) { "USE_SUDO_DOCKER=1 " } else { "" }
    $RemoteCleanup = "trap `"rm -rf -- '$RemoteStage'; rm -f -- '$RemoteArchive'`" EXIT"
    $RemoteCommand = @(
        "set -e",
        $RemoteCleanup,
        "rm -rf -- $(Quote-BashArg $RemoteStage)",
        "mkdir -p -- $(Quote-BashArg $RemoteStage)",
        "tar -xzf $(Quote-BashArg $RemoteArchive) -C $(Quote-BashArg $RemoteStage)",
        ($SudoDocker + "bash " + (Quote-BashArg "$RemoteStage/scripts/deploy_production.sh") +
            " --source " + (Quote-BashArg $RemoteStage) +
            " --target " + (Quote-BashArg $RemoteDir) +
            " --revision manual-$Stamp")
    ) -join "; "
    Run-Cmd "ssh" @("-p", [string]$ServerPort, $RemoteTarget, $RemoteCommand)

    Step "Deploy finished and healthcheck passed"
}
finally {
    if (Test-Path -LiteralPath $TempDir) {
        $ResolvedTemp = [System.IO.Path]::GetFullPath($TempDir)
        $TempRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
        if ($ResolvedTemp.StartsWith($TempRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
            Remove-Item -LiteralPath $ResolvedTemp -Recurse -Force
        }
    }
}
