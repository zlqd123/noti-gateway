# 向 ZCode 输入框注入文本（默认“继续”）并回车发送。
# 安全设计：
#   1. 激活目标窗口后校验前台句柄，校验不过绝不发键（防止打到别的应用）
#   2. 用剪贴板粘贴（绕开中文输入法），结束后还原剪贴板原文本内容
param(
  [string]$Text = "继续",
  [string]$TextFile = "",   # 中文经命令行参数会乱码，长文本走 UTF-8 文件传入
  [string]$TargetProcess = "ZCode"
)
$ErrorActionPreference = 'Stop'
if ($TextFile -and (Test-Path $TextFile)) { $Text = [System.IO.File]::ReadAllText($TextFile, [System.Text.Encoding]::UTF8) }
Add-Type -AssemblyName System.Windows.Forms
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class Win32Gate {
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
}
"@

$proc = Get-Process -Name $TargetProcess -ErrorAction Stop | Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
if (-not $proc) { Write-Output "NO_WINDOW"; exit 3 }
$hwnd = $proc.MainWindowHandle

[Win32Gate]::ShowWindow($hwnd, 9) | Out-Null    # SW_RESTORE（最小化时还原）
Start-Sleep -Milliseconds 150
[Win32Gate]::SetForegroundWindow($hwnd) | Out-Null
Start-Sleep -Milliseconds 450
if ([Win32Gate]::GetForegroundWindow() -ne $hwnd) { Write-Output "FOCUS_FAIL"; exit 2 }

function Set-ClipRetry([string]$v) {
  for ($i = 0; $i -lt 4; $i++) {
    try { Set-Clipboard -Value $v; return $true } catch { Start-Sleep -Milliseconds 150 }
  }
  return $false
}

$oldClip = $null
try { $oldClip = Get-Clipboard } catch {}
if (-not (Set-ClipRetry $Text)) { Write-Output "CLIPBOARD_FAIL"; exit 4 }
Start-Sleep -Milliseconds 150
[System.Windows.Forms.SendKeys]::SendWait("^v")
Start-Sleep -Milliseconds 350
[System.Windows.Forms.SendKeys]::SendWait("{ENTER}")
Start-Sleep -Milliseconds 600
# 焦点仍在本窗口时补发第二次回车：第一次若被焦点竞态吃掉，第二次补发；
# 若第一次已发送成功，空输入框上的回车是无害 no-op
if ([Win32Gate]::GetForegroundWindow() -eq $hwnd) {
  [System.Windows.Forms.SendKeys]::SendWait("{ENTER}")
  Start-Sleep -Milliseconds 300
}
if ($oldClip) { Set-ClipRetry $oldClip | Out-Null }
Write-Output "TYPED"
exit 0
