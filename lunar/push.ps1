param(
    [string]$Message = "Update project"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $RepoRoot

$currentBranch = git branch --show-current
if (-not $currentBranch) {
    $currentBranch = "master"
}

git add -A

$status = git status --short
if ($status) {
    git commit -m $Message
}

git push -u origin $currentBranch
