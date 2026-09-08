# NTOverAOA

NTOverAOA connects NetworkTables data to an Android device over USB Android
Open Accessory mode.

## Clone the repository

libwdi is included as a pinned Git submodule and is required for the Windows
build. Clone the repository with its submodules:

```bash
git clone --recurse-submodules https://github.com/aidan-j532/NTOverSerial.git
```

For an existing checkout, initialize the submodule with:

```bash
git submodule update --init --recursive
```

The Windows GitHub Actions build checks out submodules, builds libwdi's
`libwdi.dll`, and bundles it into the application. Linux builds do not build
or package libwdi.