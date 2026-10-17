# LARR-E Cam (native 3DS)

A small homebrew app for the New 3DS. It shows the outer camera live on the top screen,
with a green box in the middle. The bottom screen shows:

- **bright**: the average brightness (0–255) inside the green box. Point the box at the
  vial. A cloudy (turbid) culture scatters more light than clear medium, so this is a crude
  turbidity reading. It is a stand-in for a real model.
- **fps**: frames captured and shown per second (the camera is set to 30 fps).
- **classify**: how long `classify_frame` took, in milliseconds.
- **LARR-E**, when it is linked: one line per unit (OD, temperature, growth rate, stirring;
  "old" when the readings are stale), the latest alert from the last 10 minutes (red when
  critical), and any hardware action waiting for approval. **Ⓐ approves, Ⓑ denies** the
  action shown. Decisions are logged as made by `3ds`.

## Link it to LARR-E

The app talks straight to `larr-e api` over Wi-Fi, so it doesn't need the PocketJS bridge.
Put a file `/3ds/larre.cfg` on the SD card:

```
url=http://192.168.43.10:8787
token=<the same LARRE_API_TOKEN as larr-e api>
```

To bundle the settings into the app instead, put the same two lines in `romfs/larre.cfg`
(gitignored, since it holds the token) and run `make clean && make`. A file on the SD card
still wins over the bundled one.

`url` is the computer running `larr-e api`, on the same network as the 3DS. Plain `http://`
only. The app polls `GET /api/screen` every 2 s (a plain-text view made for this 40-column
screen; see `api_server.screen_lines`) and sends decisions to `POST /api/approvals/<id>`. The
title line says `online`, `wrong token (HTTP 401)` or `no connection (...)`. Without the file
the camera still works on its own.

The parsing code (`source/larre_cfg.c`, `source/screen.c`) has no 3DS dependencies.
`tests/test_native3ds_logic.py` compiles and runs `test/test_logic.c` with the Mac's `cc`.

`classify_frame()` in [`source/classify.c`](source/classify.c) is where a small int8 neural
network (TensorFlow Lite Micro) will go. It gets one 320×240 RGB frame.

It is separate from `../pocket3ds/` (the PocketJS dashboard), which needs the bridge.

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
