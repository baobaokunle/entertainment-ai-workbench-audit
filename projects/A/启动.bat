@echo off
setlocal DisableDelayedExpansion
set "DFA_START_PYTHON=%~1"
if not [%3]==[] goto DFA_INVALID_ARGUMENTS
set "DFA_START_SCRIPT=%~dp0start.ps1"
set "DFA_START_DATA=%~2"
if [%1]==[] goto DFA_RUN
if [%2]==[] goto DFA_INVALID_ARGUMENTS
if "%~1"=="" goto DFA_INVALID_ARGUMENTS
if "%~2"=="" goto DFA_INVALID_ARGUMENTS
if not "%DFA_START_PYTHON:~1,1%"==":" goto DFA_INVALID_ARGUMENTS
:DFA_RUN
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -Command "$ErrorActionPreference='Stop'; function LocalFile($p){try{$full=[IO.Path]::GetFullPath($p);if($full -notmatch '^[A-Za-z]:\\'){return $false};$root=[IO.Path]::GetPathRoot($full);if(([IO.DriveInfo]::new($root)).DriveType -ne [IO.DriveType]::Fixed){return $false};$parts=[Collections.Generic.List[string]]::new();$part=$full;while($part -and $part -ne $root){$parts.Add($part);$part=[IO.Path]::GetDirectoryName($part)};$parts.Add($root);for($i=$parts.Count-1;$i -ge 0;$i--){if(([IO.File]::GetAttributes($parts[$i]) -band [IO.FileAttributes]::ReparsePoint) -ne 0){return $false}};return [IO.File]::Exists($full)}catch{return $false}}; $security=[IO.Path]::Combine($PSHOME,'Modules\Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1');if(-not (LocalFile $security)){[Console]::Error.WriteLine('Local policy module unavailable.');exit 1};Import-Module $security -ErrorAction Stop;if((Get-ExecutionPolicy -Scope MachinePolicy) -ne 'Undefined' -or (Get-ExecutionPolicy -Scope UserPolicy) -ne 'Undefined'){[Console]::Error.WriteLine('Managed execution policy detected. Ask for approved signed entry; no engine switch.');exit 1};if(-not (LocalFile $env:DFA_START_SCRIPT)){[Console]::Error.WriteLine('Launcher must be on a local fixed disk without links.');exit 1};$engine=$null;$candidates=@([IO.Path]::Combine($env:ProgramFiles,'PowerShell\7\pwsh.exe'),[IO.Path]::Combine($env:USERPROFILE,'.cache\codex-runtimes\codex-primary-runtime\dependencies\native\powershell\pwsh.exe'));foreach($candidate in $candidates){if(LocalFile $candidate){$engine=$candidate;break}};if(-not $engine){$engine=[IO.Path]::Combine($env:SystemRoot,'System32\WindowsPowerShell\v1.0\powershell.exe');if(-not (LocalFile $engine)){[Console]::Error.WriteLine('Local PowerShell unavailable.');exit 1}}; if($env:DFA_START_PYTHON){ & $engine -NoLogo -NoProfile -File $env:DFA_START_SCRIPT -PythonExe $env:DFA_START_PYTHON -DataRoot $env:DFA_START_DATA }else{ & $engine -NoLogo -NoProfile -File $env:DFA_START_SCRIPT }; exit $LASTEXITCODE"
set "DFA_START_EXIT=%errorlevel%"
if "%DFA_START_EXIT%"=="0" exit /b 0
goto DFA_FAILED
:DFA_INVALID_ARGUMENTS
set "DFA_START_EXIT=2"
echo This double-click entry accepts no arguments or exactly PythonExe DataRoot.
:DFA_FAILED
echo.
echo Startup did not complete. Exit code: %DFA_START_EXIT%
echo Review the error above. Press any key to close this window.
pause >nul
exit /b %DFA_START_EXIT%
