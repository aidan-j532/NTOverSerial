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


def toggle_accessory_mode(
    device,
    manufacturer,
    model,
    description,
    version,
    uri,
    sn,
):
    protocol = device.ctrl_transfer(
        usb.util.CTRL_TYPE_VENDOR | usb.util.CTRL_IN,
        51,
        0,
        0,
        2,
    )

    protocol_version = int(protocol[0]) | (int(protocol[1]) << 8)
    print(f"AOA GET_PROTOCOL -> {protocol!r}")
    print(f"AOA PROTOCOL VERSION -> {protocol_version}")

    if protocol_version < 1:
        raise RuntimeError(
            "Connected USB device does not support Android Open Accessory mode"
        )

    strings = (
        (0, manufacturer),
        (1, model),
        (2, description),
        (3, version),
        (4, uri),
        (5, sn),
    )

    for index, value in strings:
        try:
            result = device.ctrl_transfer(
                usb.util.CTRL_TYPE_VENDOR | usb.util.CTRL_OUT,
                52,
                0,
                index,
                value,
            )
            print(f"AOA SET_STRING value={value!r} succeeded: {result!r}")
        except Exception as e:
            print(f"AOA SET_STRING value={value!r} failed: {e}")

    print("AOA START_ACCESSORY")
    device.ctrl_transfer(
        usb.util.CTRL_TYPE_VENDOR | usb.util.CTRL_OUT,
        53,
        0,
        0,
        None,
    )
    print("AOA START_ACCESSORY returned")

    # The phone should now disconnect/re-enumerate.
    # Do not continue using the old PyUSB device object.
    try:
        usb.util.dispose_resources(device)
    except Exception:
        pass