# Look at this file in the preview view, it is up where the run button normall is
# Todo
- It doesn't run on linux, 
```
traceback (most recent call last):
  File "/home/eprig/Documents/NTOverSerial/run.py", line 10, in <module>
    from classes.usb_handler import USBHandler
  File "/home/eprig/Documents/NTOverSerial/classes/usb_handler.py", line 9, in <module>
    from libusb._platform.windows import DLL_PATH
  File "/home/eprig/.local/share/mise/installs/python/3.14.7/lib/python3.14/site-packages/libusb/_platform/windows/__init__.py", line 10, in <module>
    from utlx.platform.windows import winapi
  File "/home/eprig/.local/share/mise/installs/python/3.14.7/lib/python3.14/site-packages/utlx/platform/windows/__init__.py", line 6, in <module>
    from . import capi
  File "/home/eprig/.local/share/mise/installs/python/3.14.7/lib/python3.14/site-packages/utlx/platform/windows/capi.py", line 11, in <module>
    from ctypes import WinDLL as DLL
ImportError: cannot import name 'WinDLL' from 'ctypes' (/home/eprig/.local/share/mise/installs/python/3.14.7/lib/python3.14/ctypes/__init__.py)
```

## Completed
- Create Reusable Logging Function - Done (u can call TKApp._log(msg)/self._log(msg))
- Create Reusable NT Send/Receive Client - Done (NTHandler in nt_handler.py)
- Create Reusable USB Send/Receive Client - Done (USBHandler in usb_handler.py)
- Fix Application Freezing when trying to connect/disconnect i think i fixed but i cant test here
- Add Check to make sure proper usb device is selected, avoid changing drivers for the xbox controllers (it kinda works, it only changes the driver for Android devices, and require you to say yes to changing the devices driver)

## In progress
- Create Subscription Store (Array, or hash map?), not StructDataStuff
- Setup building to exe, use [pyinstaller](pyinstaller.org)
- Add Settings menu to install apk on android device
