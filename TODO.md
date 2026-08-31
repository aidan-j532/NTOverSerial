# Todo
- Remove read_usb_line from nt_handler, nt_handler should purely handle nt, not usb
- Unable to use, devices not showing up when phone is connected, error message `Device scan failed: USB enumeration failed: 'Device' object has no attribute 'configs'`

## Completed
- Create Reusable Logging Function - Done (u can call TKApp._log(msg)/self._log(msg))
- Create Reusable NT Send/Receive Client - Done (NTHandler in nt_handler.py)
- Create Reusable USB Send/Receive Client - Done (USBHandler in usb_handler.py)
- Fix Application Freezing when trying to connect/disconnect i think i fixed but i cant test here

## In progress
- Create Subscription Store (Array, or hash map?), not StructDataStuff
- Setup building to exe, use [pyinstaller](pyinstaller.org)
- Add Settings menu to install apk on android device
- Add Check to make sure proper usb device is selected, avoid changing drivers for the xbox controllers