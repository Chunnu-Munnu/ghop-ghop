# Shared helper: find a Python interpreter that can already run the backend.
# Dot-source it:  . (Join-Path $PSScriptRoot 'python-env.ps1')

$script:BackendImports = 'import starlette, uvicorn, serial, numpy, scipy'

function Find-BackendPython {
    param([Parameter(Mandatory)][string]$Backend)

    $specs = New-Object 'System.Collections.Generic.List[string[]]'
    $venv = Join-Path $Backend '.venv\Scripts\python.exe'
    if (Test-Path $venv) { $specs.Add([string[]]@($venv)) }
    foreach ($version in '3.14', '3.13', '3.12', '3.11', '3.10') { $specs.Add([string[]]@('py', "-$version")) }
    $specs.Add([string[]]@('python'))

    foreach ($spec in $specs) {
        if (-not (Get-Command $spec[0] -ErrorAction SilentlyContinue)) { continue }
        $prefix = @($spec | Select-Object -Skip 1)
        try {
            & $spec[0] @prefix -c $script:BackendImports 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) { return $spec }
        } catch {
            continue
        }
    }
    return @()
}
