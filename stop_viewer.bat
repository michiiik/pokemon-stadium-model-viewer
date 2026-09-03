@echo off
setlocal

set "PORT=8767"
echo Stopping PMS Model Viewer processes using port %PORT%...

powershell -NoProfile -ExecutionPolicy Bypass -Command "$port = '%PORT%'; $procs = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine -match 'stadium1_viewer\.py' -and $_.CommandLine -match ('--port\s+' + $port) }; if ($procs) { $procs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; Write-Host ('Stopped PID ' + $_.ProcessId) } } else { Write-Host 'No matching viewer process found.' }"

endlocal
