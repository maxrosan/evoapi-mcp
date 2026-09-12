# Remove a tarefa e encerra o executor, se estiver rodando.
$nome = 'EVOAPI Executor'
Stop-ScheduledTask -TaskName $nome -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $nome -Confirm:$false -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe' OR Name = 'python.exe'" |
    Where-Object { $_.CommandLine -like '*watch.py*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Write-Host "Tarefa '$nome' removida."
