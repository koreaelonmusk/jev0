$ErrorActionPreference = "Stop"

# Run from a reviewed checkout. No network requests and no profile/PATH edits.
$sourceDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$source = Join-Path $sourceDir "jev0.py"

$pythonCommand = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCommand) {
    Write-Error "jev0: Python 3.9+ is required"
    exit 1
}
$python = $pythonCommand.Source
& $python -c "import sys; raise SystemExit(sys.version_info < (3, 9))"
if ($LASTEXITCODE -ne 0) {
    Write-Error "jev0: Python 3.9+ is required"
    exit 1
}

$target = if ($env:JEV0_BIN_DIR) {
    [Environment]::ExpandEnvironmentVariables($env:JEV0_BIN_DIR)
} else {
    Join-Path $HOME ".local\bin"
}
$target = [IO.Path]::GetFullPath($target)
[IO.Directory]::CreateDirectory($target) | Out-Null

$destinationScript = Join-Path $target "jev0.py"
$destinationCmd = Join-Path $target "jev0.cmd"
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$wrapper = "@echo off`r`n`"$python`" `"%~dp0jev0.py`" %*`r`n"

function Test-ReparsePoint([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) {
        return $false
    }
    $item = Get-Item -LiteralPath $Path -Force
    return (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)
}

function Test-SameFile([string]$Left, [string]$Right) {
    if (-not (Test-Path -LiteralPath $Left) -or -not (Test-Path -LiteralPath $Right)) {
        return $false
    }
    $leftHash = (Get-FileHash -LiteralPath $Left -Algorithm SHA256).Hash
    $rightHash = (Get-FileHash -LiteralPath $Right -Algorithm SHA256).Hash
    return $leftHash -eq $rightHash
}

$tempScript = Join-Path $target (".jev0-install-" + [Guid]::NewGuid().ToString("N") + ".py")
$tempCmd = Join-Path $target (".jev0-install-" + [Guid]::NewGuid().ToString("N") + ".cmd")
$installedNow = New-Object System.Collections.Generic.List[string]

try {
    [IO.File]::Copy($source, $tempScript, $false)
    [IO.File]::WriteAllText($tempCmd, $wrapper, $utf8NoBom)

    foreach ($pair in @(
        @($destinationScript, $tempScript),
        @($destinationCmd, $tempCmd)
    )) {
        $destination = $pair[0]
        $candidate = $pair[1]

        if (Test-Path -LiteralPath $destination) {
            if (Test-ReparsePoint $destination) {
                throw "jev0: existing install target is a reparse point; preserved for review: $destination"
            }
            if (-not (Test-SameFile $destination $candidate)) {
                throw "jev0: existing install target differs; preserved for review: $destination"
            }
            continue
        }

        try {
            [IO.File]::Move($candidate, $destination)
            $installedNow.Add($destination)
        } catch {
            # Another process may have won the race. Accept only an identical,
            # non-reparse destination; otherwise fail closed.
            if (
                (Test-Path -LiteralPath $destination) -and
                -not (Test-ReparsePoint $destination) -and
                (Test-SameFile $destination $candidate)
            ) {
                continue
            }
            throw
        }
    }

    Write-Output "Installed $destinationCmd; add $target to PATH if needed."
} catch {
    foreach ($path in $installedNow) {
        if (Test-Path -LiteralPath $path) {
            Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
        }
    }
    Write-Error $_
    exit 1
} finally {
    foreach ($temp in @($tempScript, $tempCmd)) {
        if (Test-Path -LiteralPath $temp) {
            Remove-Item -LiteralPath $temp -Force -ErrorAction SilentlyContinue
        }
    }
}
