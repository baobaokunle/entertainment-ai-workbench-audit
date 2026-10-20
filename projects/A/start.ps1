[CmdletBinding()]
param(
    [string]$PythonExe,
    [string]$DataRoot,
    [string]$LaunchProfile,
    [ValidateSet('serve-readonly','serve','check')][string]$Mode,
    [string]$CandidateReviewConfig,
    [string]$ModelAssets,
    [switch]$AiWorker,
    [switch]$StartModelEngine,
    [string]$OperatorKey,
    [string]$Account,
    [switch]$PairLocal,
    [switch]$WebLogin,
    [switch]$LocalNotes,
    [switch]$LocalReply,
    [string]$ExtensionId,
    [int]$PairingDuration,
    [switch]$PlanOnly
)
$ErrorActionPreference='Stop'
if ($args.Count -ne 0) { Write-Error '启动入口不接受未声明的参数' -ErrorAction Continue; exit 2 }
function Assert-NoLinks([string]$Literal) {
    $current=Get-Item -LiteralPath $Literal -Force
    while ($null -ne $current) {
        if (($current.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw '启动路径不允许链接或目录联接' }
        if ($current -is [IO.DirectoryInfo]) { $current=$current.Parent } else { $current=$current.Directory }
    }
}
function Read-FiniteJson([string]$Literal) {
    Assert-NoLinks $Literal
    if ((Get-Item -LiteralPath $Literal).Length -gt 524288) { throw '公共运行清单超过大小限制' }
    return (Get-Content -LiteralPath $Literal -Raw -Encoding UTF8 | ConvertFrom-Json)
}
try {
    if (!$PythonExe) {
        $lockFile=Join-Path $PSScriptRoot 'runtime-lock.json'
        if (!(Test-Path -LiteralPath $lockFile -PathType Leaf)) { throw 'F盘公共运行环境尚未准备；不寻找C盘旧工程或系统PATH' }
        $runtimeLock=Read-FiniteJson $lockFile
        if ($runtimeLock.format -ne 'dfa-python-runtime-lock-1' -or $runtimeLock.bits -ne 64 -or $runtimeLock.python_relative -notmatch '^runtime/python-3\.12\.[0-9]+/python\.exe$' -or $runtimeLock.python_sha256 -notmatch '^[0-9a-f]{64}$' -or $runtimeLock.manifest_sha256 -notmatch '^[0-9a-f]{64}$') { throw 'F盘运行锁格式无效' }
        $PythonExe=Join-Path $PSScriptRoot $runtimeLock.python_relative
        Assert-NoLinks $PythonExe
        if ((Get-FileHash -LiteralPath $PythonExe -Algorithm SHA256).Hash -ne $runtimeLock.python_sha256) { throw 'F盘解释器文件摘要不符，未启动' }
        $runtimeDirectory=Split-Path -Parent $PythonExe
        $manifestFile=Join-Path $runtimeDirectory 'RUNTIME_MANIFEST.json'
        Assert-NoLinks $manifestFile
        if ((Get-FileHash -LiteralPath $manifestFile -Algorithm SHA256).Hash -ne $runtimeLock.manifest_sha256) { throw '公共运行清单摘要不符，未启动' }
        $manifest=Read-FiniteJson $manifestFile
        if ($manifest.format -ne 'dfa-public-python-runtime-1' -or $manifest.bits -ne 64 -or $manifest.version -ne $runtimeLock.version -or @($manifest.files).Count -lt 100 -or @($manifest.files).Count -gt 2000) { throw '公共运行清单结构无效' }
        $prefix=[IO.Path]::GetFullPath($runtimeDirectory).TrimEnd([IO.Path]::DirectorySeparatorChar)+[IO.Path]::DirectorySeparatorChar
        $seen=[Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
        foreach ($entry in $manifest.files) {
            if ($entry.file -notmatch '^[A-Za-z0-9_./-]+$' -or $entry.file -match '(^|/)\.\.(/|$)' -or $entry.sha256 -notmatch '^[0-9a-f]{64}$' -or !$seen.Add([string]$entry.file)) { throw '公共运行清单文件字段无效' }
            $target=[IO.Path]::GetFullPath((Join-Path $runtimeDirectory $entry.file))
            if (!$target.StartsWith($prefix,[StringComparison]::OrdinalIgnoreCase)) { throw '公共运行文件超出F盘目录' }
            Assert-NoLinks $target
            if ((Get-Item -LiteralPath $target).Length -ne $entry.bytes -or (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash -ne $entry.sha256) { throw 'F盘公共运行组件校验未通过，未启动' }
        }
        # -I still imports site. Reject unlisted startup components and inspect
        # directories before descending, so a junction cannot hide extra files.
        $expectedDirectories=[Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
        foreach ($entry in $manifest.files) {
            $parts=([string]$entry.file).Split('/')
            for ($index=1;$index -lt $parts.Length;$index++) { $null=$expectedDirectories.Add(($parts[0..($index-1)] -join '/')) }
        }
        $null=$seen.Add('RUNTIME_MANIFEST.json')
        $actual=[Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
        $directories=[Collections.Generic.Queue[string]]::new();$directories.Enqueue($runtimeDirectory)
        while ($directories.Count -gt 0) {
            foreach ($item in (Get-ChildItem -LiteralPath $directories.Dequeue() -Force)) {
                if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw '公共运行目录出现未允许的链接，未启动' }
                $relative=$item.FullName.Substring($prefix.Length).Replace([IO.Path]::DirectorySeparatorChar,'/')
                if ($item.PSIsContainer) {
                    if (!$expectedDirectories.Contains($relative)) { throw '公共运行环境出现额外目录，未启动' }
                    $directories.Enqueue($item.FullName)
                } else {
                    if (!$seen.Contains($relative) -or !$actual.Add($relative)) { throw '公共运行环境出现额外文件，未启动' }
                }
            }
        }
        if (!$actual.SetEquals($seen)) { throw '公共运行文件集合不完整，未启动' }
    }
    if (![IO.Path]::IsPathRooted($PythonExe) -or !(Test-Path -LiteralPath $PythonExe -PathType Leaf)) { throw '必须使用明确的已核验Python3.12 x64解释器' }
    Assert-NoLinks $PythonExe
    $launchArgs=@('-X','utf8','-I','-B',(Join-Path $PSScriptRoot 'tools/launch_workbench.py'))
    # Preserve explicit false switches as override intent. Profile v2 must not
    # silently turn a disabled command-line selection back on from a file.
    if ($LaunchProfile -or !$DataRoot) {
        $runtimeOptions=@('DataRoot','Mode','CandidateReviewConfig','ModelAssets','AiWorker','StartModelEngine','OperatorKey','Account','PairLocal','WebLogin','LocalNotes','LocalReply','ExtensionId','PairingDuration')
        foreach ($option in $runtimeOptions) {
            if ($PSBoundParameters.ContainsKey($option)) { $launchArgs+='--profile-options-explicit'; break }
        }
    }
    if ($DataRoot) { $launchArgs+=@('--data-root',$DataRoot) }
    if ($LaunchProfile) { $launchArgs+=@('--launch-profile',$LaunchProfile) }
    if ($Mode) { $launchArgs+=@('--mode',$Mode) }
    if ($CandidateReviewConfig) { $launchArgs+=@('--candidate-review-config',$CandidateReviewConfig) }
    if ($ModelAssets) { $launchArgs+=@('--model-assets',$ModelAssets) }
    if ($AiWorker) { $launchArgs+='--ai-worker' }
    if ($StartModelEngine) { $launchArgs+='--start-model-engine' }
    if ($LocalNotes) { $launchArgs+=@('--local-notes','--protection-shell',(Join-Path $PSHOME 'pwsh.exe')) }
    if ($LocalReply) { $launchArgs+='--local-reply' }
    if ($OperatorKey) { $launchArgs+=@('--operator-key',$OperatorKey) }
    if ($Account) { $launchArgs+=@('--account',$Account) }
    if ($PairLocal -and $WebLogin -and -not $LocalNotes) {throw '同时网页登录与扩展配对须明确有限本地模式'}
    if ($PairLocal -or $WebLogin) {
        if ($PairLocal) {$launchArgs+=@('--pair-local','--extension-id',$ExtensionId)}
        if ($WebLogin) {if ($ExtensionId -and -not $PairLocal) {throw '单独网页登录不接受扩展ID参数'}; $launchArgs+='--web-login'}
        $launchArgs+=@('--pairing-duration',[string]$PairingDuration,'--pairing-ui-shell',(Join-Path $PSHOME 'pwsh.exe'))
    } elseif ($ExtensionId -or $PSBoundParameters.ContainsKey('PairingDuration')) {throw '配对参数需明确PairLocal'}
    if ($PlanOnly) { $launchArgs+='--plan-only' }
    & $PythonExe @launchArgs
    exit $LASTEXITCODE
} catch {
    Write-Error '本机启动未完成，已停止；不回退旧工程、不自动创建资料或切换业务。' -ErrorAction Continue
    exit 1
}
