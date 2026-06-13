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

    [string]$Result = "成功"
)

$logPath = "E:\test\THREAD_SYNC_LOG.md"
$time = Get-Date -Format "yyyy-MM-dd HH:mm:ss K"

$entry = @"

时间: $time
线程: $Thread
阶段: $Phase
文件: $Files
摘要: $Summary
结果: $Result
"@

# 简单文件锁，降低并发线程同时写日志时的冲突概率
$maxRetry = 20
$retry = 0
while ($true) {
    try {
        $fs = [System.IO.File]::Open($logPath, [System.IO.FileMode]::OpenOrCreate, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
        try {
            $fs.Seek(0, [System.IO.SeekOrigin]::End) | Out-Null
            $bytes = [System.Text.Encoding]::UTF8.GetBytes($entry)
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
            throw "无法写入日志（可能被其他线程长时间占用）：$logPath"
        }
        Start-Sleep -Milliseconds 150
    }
}

Write-Output "日志已写入: $logPath"
