param(
    [switch]$Check,

    [Alias('Yes')]
    [switch]$Force
)

$ErrorActionPreference = 'Continue'

# grabs every present 18D1:2D00 / 18D1:2D01 device (the phone in accessory mode)
function Get-AccessoryDevices {
    Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue | Where-Object {
        $_.InstanceId -match '^USB\\VID_18D1&PID_2D0[01]'
    }
}

# find zadig.exe: next to this script, in common spots, then PATH
function Find-Zadig {
    $here = Split-Path -Parent $MyInvocation.MyCommand.Path
    $candidates = @(
        (Join-Path $here 'zadig.exe'),
        "$env:USERPROFILE\Downloads\zadig.exe",
        "$env:USERPROFILE\Desktop\zadig.exe",
        "$env:USERPROFILE\Documents\zadig.exe"
    )
    foreach ($p in $candidates) {
        if (Test-Path -LiteralPath $p) { return $p }
    }
    $onPath = Get-Command zadig.exe -ErrorAction SilentlyContinue
    if ($onPath) { return $onPath.Source }
    return $null
}

# write a zadig.ini right next to the exe so it picks up our settings
function Write-Ini {
    param([string]$ExePath)
    $dir = Split-Path -Parent $ExePath
    $ini = @"
[general]
  advanced_mode = true
  exit_on_success = true
  log_level = 0

[device]
  list_all = true

[driver]
  default_driver = 0
"@
    Set-Content -LiteralPath (Join-Path $dir 'zadig.ini') -Value $ini -Encoding ASCII
}

# a preset device file so Zadig pre-selects our accessory device
function Write-Preset {
    param([string]$ExePath)
    $dir = Split-Path -Parent $ExePath
    $cfg = @"
# auto-targets the NTOverAOA accessory device.
# if the dropdown ends up on the wrong interface (eg ADB), pick the one
# that shows the vendor-specific accessory interface before installing.
[device]
  Description = "NTOverAOA Android Accessory"
  VID = 0x18D1
  PID = 0x2D00
"@
    Set-Content -LiteralPath (Join-Path $dir 'NTOverAOA.cfg') -Value $cfg -Encoding ASCII
}

# return the pressed key, or $null if none / no interactive console
function Test-KeyPressed {
    try {
        if ([Console]::KeyAvailable) {
            return [Console]::ReadKey($true).Key
        }
    } catch {
        # redirected input - key polling unavailable, countdown must simply run
    }
    return $null
}

# warn, then wait up to 5 seconds before allowing the driver change.
# Enter proceeds now, any other key or Ctrl+C aborts, timeout proceeds.
function Confirm-DriverChange {
    param(
        [Parameter(Mandatory = $true)]
        [object[]]$Devices,
        [switch]$Force
    )

    if ($Force) {
        Write-Host "Driver change forced (-Force), skipping the countdown."
        return $true
    }

    $abort = $false
    $handler = [ConsoleCancelEventHandler]{
        param($sender, $eventArgs)
        $eventArgs.Cancel = $true
        $script:abort = $true
    }
    [Console]::add_CancelKeyPress($handler)

    try {
        Write-Host ""
        Write-Host "================ WARNING ================"
        Write-Host "This replaces the Windows driver with WinUSB for:"
        Write-Host ""
        foreach ($d in $Devices) {
            $srv = if ($null -eq $d.DriverService) { '(none)' } else { $d.DriverService }
            Write-Host "  * $($d.FriendlyName)"
            Write-Host "      Instance : $($d.InstanceId)"
            Write-Host "      Status   : $($d.Status)"
            Write-Host "      Driver   : $srv"
        }
        Write-Host ""
        Write-Host "Only Android accessory devices (VID 18D1 / PID 2D00|2D01) are targeted."
        Write-Host "Press ENTER to proceed now, any other key to abort,"
        Write-Host "or wait for the countdown."

        for ($i = 5; $i -ge 1; $i--) {
            if ($abort) {
                Write-Host ""
                Write-Host "Aborted (Ctrl+C)."
                return $false
            }

            $key = Test-KeyPressed

            if ($key -ne $null) {
                if ($key -eq [ConsoleKey]::Enter) {
                    Write-Host ""
                    Write-Host "Proceeding now."
                    return $true
                }

                Write-Host ""
                Write-Host "Aborted by keypress."
                return $false
            }

            Write-Host -NoNewline "`r  $i..."
            Start-Sleep -Seconds 1
        }

        Write-Host ""
        Write-Host "Proceeding automatically."
        return $true
    }
    finally {
        [Console]::remove_CancelKeyPress($handler)
    }
}

$devs = @(Get-AccessoryDevices)

if ($devs.Count -eq 0) {
    if ($Check) {
        Write-Host "No accessory device (18D1:2D00/2D01) present."
        Write-Host "The phone is not in accessory mode yet, or is not connected."
    } else {
        Write-Host "No accessory device (18D1:2D00/2D01) found."
        Write-Host ""
        Write-Host "The phone has to be in accessory mode FIRST:"
        Write-Host "  1. Enable USB debugging on the phone (Settings > About > tap Build number 7x > Developer options > USB debugging)."
        Write-Host "  2. Run:  .\env\Scripts\python.exe run.py"
        Write-Host "  3. Refresh, select the phone, press Connect and Run."
        Write-Host "     The handshake runs; endpoint discovery may still fail. That is expected."
        Write-Host "  4. Re-run this script."
    }
    exit 1
}

$zadig = Find-Zadig
if (-not $zadig) {
    Write-Host "Could not find zadig.exe."
    Write-Host "Download it from:  https://zadig.akeo.ie/"
    Write-Host "Put zadig.exe next to this script (or in Downloads) and re-run."
    exit 1
}

$deviceInfo = @()

foreach ($d in $devs) {
    $svc = (Get-PnpDeviceProperty -InstanceId $d.InstanceId -KeyName 'DEVPKEY_Device_DriverService' -ErrorAction SilentlyContinue).Data

    if ($d.Status -eq 'OK' -and $svc -eq 'WinUSB') {
        Write-Host "Already bound to WinUSB: $($d.FriendlyName)  [$($d.InstanceId)]"
        continue
    }

    Write-Host "Needs WinUSB: $($d.FriendlyName)  [$($d.InstanceId)]  Status: $($d.Status)  Driver: $svc"

    if ($Check) {
        continue
    }

    $deviceInfo += [pscustomobject]@{
        FriendlyName  = $d.FriendlyName
        InstanceId    = $d.InstanceId
        Status        = $d.Status
        DriverService = $svc
    }
}

if ($deviceInfo.Count -eq 0) {
    if ($Check) {
        Write-Host "All Android accessory devices (18D1:2D00/2D01) are bound to WinUSB. Ready."
        exit 0
    }

    Write-Host "All Android accessory devices are already bound to WinUSB. Nothing to do."
    exit 0
}

if ($Check) {
    Write-Host "Some Android accessory devices still need the WinUSB driver."
    exit 1
}

Write-Ini $zadig
Write-Preset $zadig

if (-not (Confirm-DriverChange -Devices $deviceInfo -Force:$Force)) {
    Write-Host "Driver change aborted. No drivers were touched."
    exit 1
}

$present = @(Get-AccessoryDevices)
if ($present.Count -eq 0) {
    Write-Host "The accessory device disappeared during the countdown. Aborting before any driver change."
    exit 1
}

Write-Host ""
Write-Host "Zadig should open with the accessory device pre-selected and WinUSB as the target."
Write-Host "Make sure the dropdown shows the accessory interface (18D1:2D00), not ADB, then press Install."
Write-Host "It exits on its own once the driver is installed."
Write-Host "Re-run with -Check to verify:  powershell -ExecutionPolicy Bypass -File setup_accessory_driver.ps1 -Check"

& $zadig
