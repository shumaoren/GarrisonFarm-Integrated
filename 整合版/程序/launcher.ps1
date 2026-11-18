param(
    [Parameter(Mandatory)]
    [ValidateSet('medal', 'promotion', 'touch', 'gpu')]
    [string]$Mode
)

$ErrorActionPreference = 'Stop'
$encoding = [Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = $encoding
$OutputEncoding = $encoding

function Invoke-GarrisonFarm([string]$Task, [string]$Arguments = '', [string]$GpuSetting = '') {
    $info = [Diagnostics.ProcessStartInfo]::new()
    $info.FileName = Join-Path $PSScriptRoot 'GarrisonFarm.exe'
    $info.WorkingDirectory = $PSScriptRoot
    $info.Arguments = '--mumu-check ' + $Arguments
    $info.UseShellExecute = $false
    $info.RedirectStandardOutput = $true
    $info.StandardOutputEncoding = $encoding
    $info.EnvironmentVariables['GF_LAUNCH_MODE'] = $Task
    $info.EnvironmentVariables['PYTHONUTF8'] = '1'
    $info.EnvironmentVariables['PYTHONDONTWRITEBYTECODE'] = '1'
    [void]$info.EnvironmentVariables.Remove('GF_TEST_GPU_SETTING')
    if ($GpuSetting) { $info.EnvironmentVariables['GF_TEST_GPU_SETTING'] = $GpuSetting }
    $isCheck = $Task -in @('gpu-list', 'gpu-test', 'touch-check')
    $info.RedirectStandardInput = $isCheck
    $process = [Diagnostics.Process]::Start($info)
    try {
        if ($isCheck) {
            $process.StandardInput.Close()
            $output = $process.StandardOutput.ReadToEnd()
        } else {
            Write-FilteredSdkOutput $process.StandardOutput
            $output = ''
        }
        $process.WaitForExit()
        return [pscustomobject]@{ Code = $process.ExitCode; Output = $output }
    } finally {
        $process.Dispose()
    }
}

function Write-FilteredSdkOutput($Reader) {
    $buffer = ''
    $passLine = $false
    $dropLine = $false
    while (($value = $Reader.Read()) -ne -1) {
        $char = [char]$value
        if ($dropLine) {
            if ($char -eq "`n") { $dropLine = $false }
            continue
        }
        if ($passLine) {
            [Console]::Write($char)
            if ($char -eq "`n") { $passLine = $false }
            continue
        }
        $buffer += $char
        if (($buffer -match '^x: -?\d') -or ($buffer -eq 'after convert, x_point:')) {
            $dropLine = $true
            $buffer = ''
            continue
        }
        $possible = 'x: '.StartsWith($buffer) -or 'x: -'.StartsWith($buffer) -or 'after convert, x_point:'.StartsWith($buffer)
        if (-not $possible) {
            [Console]::Write($buffer)
            $passLine = ($char -ne "`n")
            $buffer = ''
        }
    }
    if (-not $dropLine -and $buffer) { [Console]::Write($buffer) }
}

function Save-Settings([string]$Path, $Setting) {
    $tmp = "$Path.$PID.tmp"
    try {
        $json = $Setting | ConvertTo-Json -Depth 5
        [IO.File]::WriteAllText($tmp, $json + "`r`n", $encoding)
        if ([IO.File]::Exists($Path)) {
            [IO.File]::Replace($tmp, $Path, [NullString]::Value)
        } else {
            [IO.File]::Move($tmp, $Path)
        }
    } finally {
        if ([IO.File]::Exists($tmp)) { [IO.File]::Delete($tmp) }
    }
}

function Set-Touch {
    $path = Join-Path $PSScriptRoot 'mumu_touch.json'
    $setting = Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json
    $test = Invoke-GarrisonFarm 'touch-check'
    Write-Host ($test.Output.Trim())
    $ready = $test.Code -eq 0 -and $test.Output.Contains('SDK 连接成功')
    if (-not $ready) { Write-Host '增强触控暂不可用。启用后，连接失败时会使用普通触控。' }
    $current = if ($setting.enabled) { '开启' } else { '关闭' }
    Write-Host "当前设置为 $current"
    Write-Host '  1. 开启  0. 关闭  回车保留设置'
    do {
        $choice = Read-Host '输入选项'
    } until ($choice -in @('1', '0', ''))
    if ($choice -ne '') {
        $setting.enabled = $choice -eq '1'
        Save-Settings $path $setting
        Write-Host '增强触控设置已保存。'
    }
}

function Get-PhysicalGpuChoices($Devices) {
    $installed = @{}
    try {
        foreach ($controller in (Get-CimInstance Win32_VideoController -ErrorAction Stop)) {
            if ($controller.PNPDeviceID -match '^PCI\\VEN_([0-9a-f]{4})&DEV_([0-9a-f]{4})&SUBSYS_([0-9a-f]{8})&REV_([0-9a-f]{2})') {
                $key = ($Matches[1, 2, 3, 4] -join ':').ToLowerInvariant()
                $installed[$key] = 1 + [int]$installed[$key]
            }
        }
    } catch {
        return $Devices
    }
    $listed = @{}
    foreach ($card in $Devices) {
        $key = $card.hardware_id
        if ($installed[$key] -eq 1 -and $listed.ContainsKey($key)) { continue }
        $listed[$key] = $true
        $card
    }
}

function Set-Gpu {
    $path = Join-Path $PSScriptRoot 'gpu_settings.json'
    $current = 'CPU'
    if (Test-Path -LiteralPath $path) {
        $saved = Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($saved.mode -eq 'default') { $current = '系统默认 GPU' }
        if ($saved.mode -eq 'adapter') { $current = $saved.name }
    }
    Write-Host "当前设置为 $current"
    $result = Invoke-GarrisonFarm 'gpu-list'
    $record = $result.Output -split '\r?\n' | Where-Object { $_.StartsWith('GPU_JSON=') } | Select-Object -First 1
    $devices = @()
    if ($result.Code -eq 0 -and $record) {
        $decoded = ConvertFrom-Json -InputObject $record.Substring(9)
        $devices = @(Get-PhysicalGpuChoices @($decoded | Where-Object { $_ }))
    } else {
        Write-Host ($result.Output.Trim())
    }
    Write-Host '  0. 关闭加速（CPU）'
    $defaultCard = $devices | Where-Object index -EQ 0 | Select-Object -First 1
    if ($defaultCard) { Write-Host "  D. 测试系统默认 GPU · 当前为 $($defaultCard.name)" }
    else { Write-Host '  D. 测试系统默认 GPU' }
    for ($i = 0; $i -lt $devices.Count; $i++) {
        $card = $devices[$i]
        Write-Host "  $($i + 1). 测试显卡 $($i + 1) · $($card.name)（DXGI $($card.index)）"
    }
    while ($true) {
        $choice = Read-Host '输入选项，回车保留设置'
        if ($choice -eq '') { return }
        if ($choice -eq '0') {
            Save-Settings $path @{ mode = 'cpu' }
            Write-Host '已设为 CPU。'
            return
        }
        if ($choice -eq 'D') {
            $setting = @{ mode = 'default' }
        } else {
            $number = 0
            if (-not [int]::TryParse($choice, [ref]$number) -or $number -lt 1 -or $number -gt $devices.Count) { continue }
            $card = $devices[$number - 1]
            $setting = @{ mode = 'adapter'; name = $card.name; index = $card.index; hardware_id = $card.hardware_id; luid = $card.luid }
        }
        $test = Invoke-GarrisonFarm 'gpu-test' -GpuSetting ($setting | ConvertTo-Json -Compress)
        Write-Host ($test.Output.Trim())
        if ($test.Code -ne 0 -or -not $test.Output.Contains('[OCR 测试通过]')) {
            Write-Host '测试失败。可以测试另一块显卡，或选择 CPU。'
            continue
        }
        if ((Read-Host '应用这次通过测试的 GPU 设置？输入 Y 开启，回车重新选择') -eq 'y') {
            Save-Settings $path $setting
            Write-Host 'GPU 设置已保存，下次启动刷取时生效。'
            return
        }
    }
}

$guard = [Threading.Mutex]::new($false, 'Local\GarrisonFarm-OcrLaunch')
$ownsGuard = $false
$status = 0
try {
    try { $ownsGuard = $guard.WaitOne(0) }
    catch [Threading.AbandonedMutexException] { $ownsGuard = $true }
    if (-not $ownsGuard -or (Get-Process -Name GarrisonFarm -ErrorAction SilentlyContinue)) {
        throw '已有 GarrisonFarm 在运行，请关闭后再启动或更改设置。'
    }
    switch ($Mode) {
        'medal' { $status = (Invoke-GarrisonFarm 'medal' '--runs 20 --menu').Code }
        'promotion' { $status = (Invoke-GarrisonFarm 'promotion' '--runs 2147483647 --profile low --yes').Code }
        'touch' { Set-Touch }
        'gpu' { Set-Gpu }
    }
} catch {
    Write-Host "运行失败  $($_.Exception.Message)"
    $status = 1
} finally {
    if ($ownsGuard) { $guard.ReleaseMutex() }
    $guard.Dispose()
}
exit $status
