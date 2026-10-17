# LARR-E Cam (native 3DS)

A small homebrew app for the New 3DS. It shows the outer camera live on the top screen,
with a green box in the middle. The bottom screen shows:

- **fps**: frames captured and shown per second (the camera is set to 30 fps).
- **brightness**: the average brightness (0–255) inside the green box. Point the box at the
  vial. A cloudy (turbid) culture scatters more light than clear medium, so this is a crude
  turbidity reading. It is a stand-in for a real model.
- **classify**: how long `classify_frame` took, in milliseconds.

`classify_frame()` in [`source/classify.c`](source/classify.c) is where a small int8 neural
network (TensorFlow Lite Micro) will go. It gets one 320×240 RGB frame.

This is separate from `../pocket3ds/` (the PocketJS dashboard) and from the Python code.

## Build

devkitPro is the standard homebrew toolchain: devkitARM (a GCC for the 3DS's ARM CPU),
libctru (the 3DS system library) and the 3DS tools (`3dsxtool`, `smdhtool`, `3dslink`).

### macOS on Apple Silicon (no sudo)

```bash
tools/fetch-devkitpro.sh
```

This downloads just the packages this app needs (about 80 MB) from devkitPro's own package
server, checks each against a pinned SHA-256, and unpacks them into `.devkitpro/` here
(about 350 MB, gitignored). Delete that folder to uninstall. Then, in each new shell:

```bash
. ./devkitpro-env.sh
```

```bash
make
```

This produces `larre-cam.3dsx`. `make clean` removes the build output. The Makefile is
devkitPro's standard 3DS template (from `3ds-examples`), with only the name and title changed.

### Any machine: devkitPro's installer

Follow the [devkitPro pacman wiki](https://devkitpro.org/wiki/devkitPro_pacman) (on macOS:
the `.pkg` from [devkitPro/pacman releases](https://github.com/devkitPro/pacman/releases/latest)),
then `sudo dkp-pacman -S 3ds-dev`. That installs to `/opt/devkitpro` and sets `DEVKITPRO` and
`DEVKITARM` at login. Then run `make`.

### Linux with Nix (untested)

`flake.nix` wraps [devkitNix](https://github.com/bandithedoge/devkitNix), which repackages
devkitPro's Docker images, so it is Linux-only. `nix develop` then `make`, or `nix build`.
A first try in a NixOS VM ran out of disk before it finished, so it has not built yet.

## Run on a 3DS

You need a 3DS with custom firmware and the Homebrew Launcher (see
[3ds.hacks.guide](https://3ds.hacks.guide)).

1. Copy `larre-cam.3dsx` to `/3ds/` on the SD card.
2. Open the Homebrew Launcher and pick **LARR-E Cam**.
3. Press START to quit.

Or send it over Wi-Fi without removing the SD card: in the Homebrew Launcher press Y
("netloader"), then on the Mac (with `devkitpro-env.sh` sourced) run:

```bash
3dslink larre-cam.3dsx
```

## Run in an emulator (Azahar)

Azahar (the successor to Citra) emulates the camera. In its settings, open the **Camera**
tab, pick the **Rear** camera and set its image source to a still image file or to the Mac's
camera. Then load `larre-cam.3dsx` from the File menu. (The Camera tab's options were checked
in Azahar's source, not tried here.) The fps and timings in an
emulator say nothing about real hardware.

## Notes

- The app turns on the New 3DS's 804 MHz mode (`osSetSpeedupEnable`). On an older 3DS it
  runs at 268 MHz.
- Camera setup follows devkitPro's
  [3ds-examples camera/video](https://github.com/devkitPro/3ds-examples/tree/master/camera/video),
  cut down to one outer camera (`SELECT_OUT1` / `PORT_CAM1`) at QVGA (`SIZE_QVGA`, 320×240)
  in RGB565. The function names and constants were checked against libctru's
  `include/3ds/services/cam.h`.
