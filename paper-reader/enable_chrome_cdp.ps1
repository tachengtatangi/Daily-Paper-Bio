# enable_chrome_cdp.ps1
# ──────────────────────────────────────────────────────────────
# 把正在运行的 Chrome 重启为 CDP 模式（--remote-debugging-port=9222）。
# 使用 --restore-last-session 恢复所有已打开标签页，不丢 session。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File enable_chrome_cdp.ps1
#
# 之后在当前 shell 里设置环境变量（或写入系统环境变量永久生效）：
#   $env:PAPER_READER_CDP_URL = "http://localhost:9222"
# ──────────────────────────────────────────────────────────────

$CDP_PORT = 9222
$CHROME_EXE = ""

# 自动探测 Chrome 路径
$candidates = @(
    "C:\Program Files\Google\Chrome\Application\chrome.exe",
    "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
)
foreach ($c in $candidates) {
    if (Test-Path $c) { $CHROME_EXE = $c; break }
}
if (-not $CHROME_EXE) {
    try {
        $reg = Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe" -ErrorAction Stop
        if (Test-Path $reg.'(default)') { $CHROME_EXE = $reg.'(default)' }
    } catch {}
}
if (-not $CHROME_EXE) {
    Write-Host "[ERROR] Chrome not found. Set CHROME_EXE env var and retry." -ForegroundColor Red
    exit 1
}
Write-Host "[Chrome] $CHROME_EXE" -ForegroundColor Cyan

# 检查端口是否已被 CDP Chrome 占用
function Test-CDPPort($port) {
    try {
        $r = Invoke-WebRequest "http://127.0.0.1:$port/json/version" -TimeoutSec 1 -UseBasicParsing -ErrorAction Stop
        return $true
    } catch { return $false }
}

if (Test-CDPPort $CDP_PORT) {
    Write-Host "[OK] Chrome already running with CDP on port $CDP_PORT" -ForegroundColor Green
    Write-Host "Set: `$env:PAPER_READER_CDP_URL = `"http://localhost:$CDP_PORT`""
    exit 0
}

# 检查 Chrome 是否在运行
$procs = Get-Process chrome -ErrorAction SilentlyContinue
if ($procs) {
    Write-Host "[Info] Closing $($procs.Count) Chrome processes and restarting with CDP..." -ForegroundColor Yellow
    Write-Host "       (--restore-last-session will restore your tabs)"
    $procs | Stop-Process -Force
    Start-Sleep -Milliseconds 1500
} else {
    Write-Host "[Info] Chrome is not running. Starting fresh with CDP..." -ForegroundColor Yellow
}

# 启动 CDP Chrome（使用真实用户 profile + restore-last-session）
$args = @(
    "--remote-debugging-port=$CDP_PORT",
    "--restore-last-session",
    "--no-first-run"
)
Start-Process $CHROME_EXE -ArgumentList $args
Write-Host "[Started] Chrome with --remote-debugging-port=$CDP_PORT" -ForegroundColor Green

# 等待 CDP 就绪（最多 15s）
Write-Host "[Waiting] for CDP to become ready..." -NoNewline
for ($i = 0; $i -lt 15; $i++) {
    Start-Sleep -Seconds 1
    Write-Host "." -NoNewline
    if (Test-CDPPort $CDP_PORT) {
        Write-Host " ready!" -ForegroundColor Green
        break
    }
}
Write-Host ""

if (Test-CDPPort $CDP_PORT) {
    Write-Host ""
    Write-Host "SUCCESS. Now set the env var in your shell:" -ForegroundColor Green
    Write-Host '  $env:PAPER_READER_CDP_URL = "http://localhost:' + $CDP_PORT + '"' -ForegroundColor Cyan
    Write-Host ""
    Write-Host "Or set it permanently (user-level):" -ForegroundColor Green
    Write-Host "  [System.Environment]::SetEnvironmentVariable('PAPER_READER_CDP_URL','http://localhost:$CDP_PORT','User')" -ForegroundColor Cyan
} else {
    Write-Host "[WARN] CDP port not responding yet. Chrome may still be starting up." -ForegroundColor Yellow
    Write-Host "Try: Invoke-WebRequest http://127.0.0.1:$CDP_PORT/json/version"
}
