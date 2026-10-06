#!/bin/sh
# Undo install.sh (and the desktop's first-run set-up): `stickies uninstall`
# removes the keybinding drop-in or runtime keys and the ~/.local/bin/stickies
# link, each only if it is ours. The plugin folder is the git checkout this
# script sits in; remove it afterwards with
#   omarchy plugin remove eastbluewizard.stickies
# Same HOME / override rules as install.sh.
#
# Notes are kept unless you pass --purge, which also deletes (no undo) the
# notes ($STICKIES_STATE, default ~/.local/state/stickies), the downloaded
# model and venv ($STICKIES_CACHE, default ~/.cache/stickies) and a .venv an
# older `stickies setup` made next to this script ($STICKIES_VENV).
set -eu
here=$(cd "$(dirname "$0")" && pwd)
for arg in "$@"; do
    case "$arg" in
        --purge) ;;
        -h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "uninstall.sh: unknown argument: $arg (only --purge)" >&2; exit 1 ;;
    esac
done
exec "$here/stickies" uninstall "$@"
