$ErrorActionPreference = 'Continue'
$env:DSWARM_PYTHON = 'C:\Projects\Agent-projects\D-Swarm\.venv\Scripts\python.exe'
$exe = 'C:\Projects\Agent-projects\D-Swarm\desktop\build\bin\d-swarm-desktop.exe'
$p = Start-Process -FilePath $exe -PassThru
Start-Sleep -Seconds 6
Write-Output ('PID=' + $p.Id)
Write-Output ('HasExited=' + $p.HasExited)
$ports = Get-NetTCPConnection -LocalPort 8000,3001 -State Listen -ErrorAction SilentlyContinue
if ($ports) {
  $ports | Select-Object LocalPort, OwningProcess | Format-Table -AutoSize | Out-String | Write-Output
} else {
  Write-Output 'NO_LISTENERS_ON_8000_OR_3001'
}
