import usb.core

ACCESSORY_IDS = [(0x18D1, 0x2D00), (0x18D1, 0x2D01)]


def find_device(known_devices):
    ids = set((v, p) for v, p in known_devices)
    return usb.core.find(custom_match=lambda d: (d.idVendor, d.idProduct) in ids)


def find_accessory():
    return usb.core.find(
        custom_match=lambda d: (d.idVendor, d.idProduct) in ACCESSORY_IDS
    )


def toggle_accessory_mode(device, manufacturer, model, description, version, uri, sn):
    # Switch the device into Android Open Accessory mode
    device.ctrl_transfer(usb.util.CTRL_TYPE_VENDOR | usb.util.CTRL_IN, 51, 0, 0, 256)

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
