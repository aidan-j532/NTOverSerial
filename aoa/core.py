import usb.core
import usb.util

# Android Open Accessory v2 devices may expose one of these PIDs depending on
# whether ADB and/or audio are enabled. These are the modes that include the
# bulk accessory interface; 2D02 and 2D03 are audio-only.
ACCESSORY_IDS = [(0x18D1, product_id) for product_id in (0x2D00, 0x2D01, 0x2D04, 0x2D05)]


def find_device(known_devices):
    ids = set((v, p) for v, p in known_devices)
    return usb.core.find(custom_match=lambda d: (d.idVendor, d.idProduct) in ids)


def find_accessory():
    return usb.core.find(
        custom_match=lambda d: (d.idVendor, d.idProduct) in ACCESSORY_IDS
    )


def toggle_accessory_mode(device, manufacturer, model, description, version, uri, sn):
    # Switch the device into Android Open Accessory mode
    protocol = device.ctrl_transfer(
        usb.util.CTRL_TYPE_VENDOR | usb.util.CTRL_IN,
        51,
        0,
        0,
        2,
    )

    if len(protocol) != 2 or int(protocol[0]) | (int(protocol[1]) << 8) < 1:
        raise RuntimeError("Connected USB device does not support Android Open Accessory mode")

    device.ctrl_transfer(
        usb.util.CTRL_TYPE_VENDOR | usb.util.CTRL_OUT, 52, 0, 0, manufacturer
    )
    device.ctrl_transfer(usb.util.CTRL_TYPE_VENDOR | usb.util.CTRL_OUT, 52, 0, 1, model)
    device.ctrl_transfer(
        usb.util.CTRL_TYPE_VENDOR | usb.util.CTRL_OUT, 52, 0, 2, description
    )
    device.ctrl_transfer(
        usb.util.CTRL_TYPE_VENDOR | usb.util.CTRL_OUT, 52, 0, 3, version
    )
    device.ctrl_transfer(usb.util.CTRL_TYPE_VENDOR | usb.util.CTRL_OUT, 52, 0, 4, uri)
    device.ctrl_transfer(usb.util.CTRL_TYPE_VENDOR | usb.util.CTRL_OUT, 52, 0, 5, sn)

    device.ctrl_transfer(usb.util.CTRL_TYPE_VENDOR | usb.util.CTRL_OUT, 53, 0, 0, None)
