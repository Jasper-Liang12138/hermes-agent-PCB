@echo off
chcp 65001 >nul
setlocal

powershell -NoProfile -ExecutionPolicy Bypass -Command "$root=[IO.Path]::GetFullPath('%~dp0').TrimEnd('\'); $agentPath=[IO.Path]::Combine($root,'agent.exe'); $agents=@(Get-CimInstance Win32_Process -Filter \"Name = 'agent.exe'\" | Where-Object { $_.ExecutablePath -and ([IO.Path]::GetFullPath($_.ExecutablePath) -ieq $agentPath) }); if ($agents.Count -eq 0) { Write-Host 'agent.exe 未运行。'; exit 0 }; $parentIds=@($agents | Select-Object -ExpandProperty ParentProcessId -Unique); foreach ($agent in $agents) { Stop-Process -Id $agent.ProcessId -Force -ErrorAction SilentlyContinue }; Start-Sleep -Milliseconds 500; foreach ($parentId in $parentIds) { $parent=Get-CimInstance Win32_Process -Filter \"ProcessId = $parentId\" -ErrorAction SilentlyContinue; if ($parent -and $parent.Name -ieq 'cmd.exe') { Stop-Process -Id $parentId -Force -ErrorAction SilentlyContinue } }; Write-Host 'agent.exe 已结束。'; exit 0"
exit /b %ERRORLEVEL%
