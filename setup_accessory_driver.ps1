param(
    [switch]$Check
)

$ErrorActionPreference = 'Continue'

function Get-AccessoryDevices {
    Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue | Where-Object {
        $_.InstanceId -match '^USB\\VID_18D1&PID_2D0[01]'
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
        Write-Host "The phone must be switched into accessory mode FIRST:"
        Write-Host "  1. Enable USB debugging on the phone (Settings > About > tap Build number 7x > Developer options > USB debugging)."
        Write-Host "  2. Run:  .\env\Scripts\python.exe run.py"
        Write-Host "  3. Refresh, select the phone, press Connect and Run."
        Write-Host "     The USB handshake will run; it may fail at endpoint discovery. That is expected."
        Write-Host "  4. Re-run this script."
        Write-Host ""
        Write-Host "If run.py shows 'Could not enter Android accessory mode' instead, the phone didn't accept the handshake"
        Write-Host "(phone must be unlocked, USB debugging on)."
    }
    exit 1
}

foreach ($d in $devs) {
    Write-Host "Found: $($d.FriendlyName)  [Instance: $($d.InstanceId)]  Status: $($d.Status)"
    $svc = (Get-PnpDeviceProperty -InstanceId $d.InstanceId -KeyName 'DEVPKEY_Device_DriverService' -ErrorAction SilentlyContinue).Data
    $desc = (Get-PnpDeviceProperty -InstanceId $d.InstanceId -KeyName 'DEVPKEY_Device_DeviceDesc' -ErrorAction SilentlyContinue).Data
    if ($d.Status -eq 'OK' -and $svc -eq 'WinUSB') {
        Write-Host "This device already has the WinUSB driver bound. Ready to use in run.py."
        continue
    }
    if ($Check) {
        Write-Host "Not bound to WinUSB yet (service: $svc / $desc)."
        exit 1
    }
    Write-Host "Launching the Update Driver wizard for this exact device..."
    Start-Process rundll32.exe -ArgumentList "newdev.dll,DriverWizard `"$($d.InstanceId)`""
    Write-Host ""
    Write-Host "In the wizard:  Browse my computer for driver software > Let me pick from a list..."
    Write-Host "> select 'Universal Serial Bus devices' > choose 'WinUsb Device' > Next."
    Write-Host "Then re-run:  powershell -ExecutionPolicy Bypass -File setup_accessory_driver.ps1 -Check"
}