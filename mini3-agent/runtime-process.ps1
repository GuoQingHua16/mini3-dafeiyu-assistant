# Process identity helpers shared by the launcher and stop script.
function Write-Mini3State {
    param($State, [string]$Path)
    $temporary = $Path + '.' + [guid]::NewGuid().ToString('N') + '.tmp'
    try {
        [IO.File]::WriteAllText($temporary, ($State | ConvertTo-Json -Depth 5), (New-Object Text.UTF8Encoding($false)))
        Move-Item -LiteralPath $temporary -Destination $Path -Force
    } finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary }
    }
}

function Get-Mini3Identity {
    param($Process)
    [pscustomobject]@{ pid=$Process.Id; started=$Process.StartTime.ToUniversalTime().Ticks.ToString(); python=$Process.Path }
}

function Test-Mini3Identity {
    param($Identity)
    try {
        $liveProcess = Get-Process -Id ([int]$Identity.pid) -ErrorAction Stop
        return ($liveProcess.StartTime.ToUniversalTime().Ticks.ToString() -eq [string]$Identity.started -and
                [string]::Equals($liveProcess.Path, [string]$Identity.python, [StringComparison]::OrdinalIgnoreCase))
    } catch { return $false }
}

function Get-Mini3Targets {
    param($State)
    $identities = @{}
    foreach ($entry in (@($State) + @($State.children))) {
        if ($entry -and (Test-Mini3Identity $entry)) { $identities[[int]$entry.pid] = $entry }
    }
    # A venv launcher may leave its real interpreter running as a child.
    $processRows = @(Get-CimInstance Win32_Process -ErrorAction Stop)
    $pending = New-Object 'System.Collections.Generic.Queue[int]'
    foreach ($key in @($identities.Keys)) { $pending.Enqueue($key) }
    while ($pending.Count) {
        $parentId = $pending.Dequeue()
        foreach ($row in $processRows) {
            if ($row.ParentProcessId -ne $parentId -or $identities.ContainsKey([int]$row.ProcessId)) { continue }
            if ($row.CommandLine -match 'fork-server|server nodaemon') { continue }
            try {
                $child = Get-Process -Id $row.ProcessId -ErrorAction Stop
                if ($child.StartTime.ToUniversalTime().Ticks -lt [long]$identities[$parentId].started) { continue }
                $identities[[int]$row.ProcessId] = Get-Mini3Identity $child
                $pending.Enqueue([int]$row.ProcessId)
            } catch { continue }
        }
    }
    @($identities.Values)
}

function Stop-Mini3Targets {
    param($Targets)
    # Every PID is checked again immediately before termination, preventing reuse.
    foreach ($entry in @($Targets | Sort-Object { [long]$_.started } -Descending)) {
        if (Test-Mini3Identity $entry) {
            Stop-Process -Id ([int]$entry.pid) -Force -ErrorAction SilentlyContinue
        }
    }
}

function Stop-Mini3DevicePlayback {
    param([string]$AdbPath)
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $AdbPath
    $startInfo.Arguments = 'shell "local_agent stop"'
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $adbProcess = New-Object System.Diagnostics.Process
    $adbProcess.StartInfo = $startInfo
    try {
        [void]$adbProcess.Start()
        $stdout = $adbProcess.StandardOutput.ReadToEndAsync()
        $stderr = $adbProcess.StandardError.ReadToEndAsync()
        if (-not $adbProcess.WaitForExit(5000)) {
            $adbProcess.Kill()
            Write-Output '电脑服务已停止；设备响应超时。'
        } elseif ($adbProcess.ExitCode -ne 0) {
            Write-Output '电脑服务已停止；设备当前未连接。'
        } else {
            Write-Output $stdout.GetAwaiter().GetResult().Trim()
        }
    } catch { Write-Output '电脑服务已停止；未能通知设备停止播放。' }
    finally { $adbProcess.Dispose() }
}
