param(
    [ValidateSet('demo','test','verify')][string]$Action = 'demo',
    [string]$PythonExe,
    [string]$Package
)
$ErrorActionPreference = 'Stop'
if (-not $PythonExe) {
    $configPath = Join-Path $PSScriptRoot 'runtime/interpreter.json'
    if (-not (Test-Path -LiteralPath $configPath)) { throw '请通过 -PythonExe 指定 Python 3.12 或更新的基础解释器。' }
    $PythonExe = (Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json).python_exe
}
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) { throw '已登记的 Python 不存在，请使用 -PythonExe 明确指定。' }
$entry = Join-Path $PSScriptRoot 'run.py'
if ($Action -eq 'verify') {
    if (-not $Package) { throw 'verify 需要 -Package 指定 B/runtime 内的数据包。' }
    & $PythonExe -I -B -S -X utf8 $entry verify $Package
} else {
    & $PythonExe -I -B -S -X utf8 $entry $Action
}
exit $LASTEXITCODE
