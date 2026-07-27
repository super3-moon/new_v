param(
    [Parameter(Mandatory = $true)]
    [string]$Thread,

    [Parameter(Mandatory = $true)]
    [ValidateSet("START", "DONE", "BLOCKED")]
    [string]$Phase,

    [Parameter(Mandatory = $true)]
    [string]$Files,

    [Parameter(Mandatory = $true)]
    [string]$Summary,

    [string]$Result = "success"
)

$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $utf8NoBom
$OutputEncoding = $utf8NoBom

$logPath = Join-Path $PSScriptRoot "THREAD_SYNC_LOG.md"
$time = Get-Date -Format "yyyy-MM-dd HH:mm:ss K"

$entry = @"

Time: $time
Thread: $Thread
Phase: $Phase
Files: $Files
Summary: $Summary
Result: $Result
"@

# 简单文件锁，降低并发线程同时写日志时的冲突概率
$maxRetry = 20
$retry = 0
while ($true) {
    try {
        $fs = [System.IO.File]::Open($logPath, [System.IO.FileMode]::OpenOrCreate, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
        try {
            $fs.Seek(0, [System.IO.SeekOrigin]::End) | Out-Null
            $bytes = $utf8NoBom.GetBytes($entry)
            $fs.Write($bytes, 0, $bytes.Length)
            $fs.Flush()
        }
        finally {
            $fs.Close()
            $fs.Dispose()
        }
        break
    }
    catch {
        $retry++
        if ($retry -ge $maxRetry) {
            throw "Unable to update the sync log because it remained locked: $logPath"
        }
        Start-Sleep -Milliseconds 150
    }
}

Write-Output "Sync log updated: $logPath"
