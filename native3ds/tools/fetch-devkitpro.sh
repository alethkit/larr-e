#!/bin/sh
# Download the minimum devkitPro toolchain for this app (devkitARM, libctru,
# 3dstools) and unpack it into native3ds/.devkitpro. No sudo, nothing outside
# this folder. About 80 MB to download, 370 MB unpacked.
#
# These are devkitPro's own macOS (Apple Silicon) packages, pinned by version
# and SHA-256 as listed in their pacman databases on 17 Oct 2026. To update,
# take the new filenames and %SHA256SUM% values from
#   https://pkg.devkitpro.org/packages/osx/arm64/dkp-osx.db  (macOS tools)
#   https://pkg.devkitpro.org/packages/dkp-libs.db           (libraries)
#
# Usage: tools/fetch-devkitpro.sh            then: . ./devkitpro-env.sh && make
set -eu

cd "$(dirname "$0")/.."
DEST="$PWD/.devkitpro"
CACHE="$DEST/cache"

if [ "$(uname -s)-$(uname -m)" != "Darwin-arm64" ]; then
	echo "This script pins Apple Silicon macOS packages. On Linux use the flake" >&2
	echo "(nix develop) or devkitPro's installer; see README.md." >&2
	exit 1
fi

OSX=https://pkg.devkitpro.org/packages/osx/arm64
LIBS=https://pkg.devkitpro.org/packages
# The server refuses curl's default user agent, so say who we are.
UA="larr-e-native3ds-fetch/1.0 (+https://github.com/alethkit/larr-e)"

PKGS="
$OSX devkitarm-gcc-16.1.0-1-arm64.pkg.tar.zst 1b0ddb9793bd3bbeda53f60e5f0b1059ad2b4103be0e1c3a0cd83402bbea28db
$OSX devkitarm-binutils-2.46.0-1-arm64.pkg.tar.zst 175fce654158fc2730380387662cd0bc3245d1441d1cc2af9d802ee93fe28ccf
$OSX 3dstools-1.3.1-3-arm64.pkg.tar.zst 3752dd464ee355c677400723f6801ee95e660e1793d68f612516554b562f6307
$OSX general-tools-1.4.4-1-arm64.pkg.tar.zst 86f3107f313d1b8e12091b1a257c3b5c2dc4ee457e129145f81fe8afb6956b02
$OSX 3dslink-0.6.3-1-arm64.pkg.tar.zst 25685cbbc959ded51acae654c92fa8c8db2aaf824ea1e921be0a1888be36ec53
$LIBS devkitARM-r68-1-any.pkg.tar.zst 7c5dd0864f7dab505fbe9cf7699100a90f15bb8563b858740064f9842db48370
$LIBS devkitarm-newlib-4.6.0.20260123-5-any.pkg.tar.zst d314c644c143bd5b104bd82cdc27602f6449cf907cb9c37ccfb8c0707182f89d
$LIBS devkitarm-rules-1.6.0-4-any.pkg.tar.zst 35023b2b5f07a2dcfb600063854c712206341fb5df3ffbaf8ff825d3cbc15fcc
$LIBS devkitarm-crtls-1.2.6-1-any.pkg.tar.zst 65faf10f62c219a1a8a3a182b0c4c56da7992599035ea32b3d79e39e79f964bb
$LIBS libctru-2.7.0-1-any.pkg.tar.zst 6bdf9734dbaea38f457d037793f9c39d8e778ac3b3c5904c583c8e2b96bada2d
"

mkdir -p "$CACHE"
echo "$PKGS" | while read -r base file sha; do
	[ -n "$file" ] || continue
	if [ ! -f "$CACHE/$file" ]; then
		echo "fetch  $file"
		curl -fsSL -A "$UA" -o "$CACHE/$file.part" "$base/$file"
		mv "$CACHE/$file.part" "$CACHE/$file"
	fi
	echo "$sha  $CACHE/$file" | shasum -a 256 -c --quiet -
	# Packages hold opt/devkitpro/...; keep only that, minus pacman metadata.
	# Meta-packages (devkitARM) have no files and are just skipped.
	if tar -tf "$CACHE/$file" | grep -q '^opt/'; then
		echo "unpack $file"
		tar -xf "$CACHE/$file" -C "$DEST" opt
	fi
done

rm -rf "$CACHE"
echo "Done. Toolchain in $DEST/opt/devkitpro"
echo "Next: . ./devkitpro-env.sh && make"
