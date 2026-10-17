# Source this (". ./devkitpro-env.sh") to use the toolchain that
# tools/fetch-devkitpro.sh unpacked into .devkitpro/.
_here="$(cd "$(dirname "${BASH_SOURCE:-$0}")" && pwd)"
export DEVKITPRO="$_here/.devkitpro/opt/devkitpro"
export DEVKITARM="$DEVKITPRO/devkitARM"
export PATH="$DEVKITPRO/tools/bin:$DEVKITARM/bin:$PATH"
unset _here
