# Registra o executor como tarefa do Windows: sobe no logon, reinicia se cair,
# não abre janela. Rode uma vez, num PowerShell normal (não precisa de administrador).
$ErrorActionPreference = 'Stop'
$aqui = Split-Path -Parent $MyInvocation.MyCommand.Path
$nome = 'EVOAPI Executor'

$pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $pythonw) { $pythonw = (Get-Command python.exe).Source }

$acao = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$aqui\watch.py`"" -WorkingDirectory $aqui
$gatilho = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$config = New-ScheduledTaskSettingsSet `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName $nome -Action $acao -Trigger $gatilho -Settings $config `
    -Description 'Vigia a fila do EVOAPI e acorda o Claude Code quando Max manda instrucao no WhatsApp.' -Force | Out-Null
Start-ScheduledTask -TaskName $nome
Write-Host "Tarefa '$nome' registrada e iniciada. Log em $aqui\.local\executor.log"
