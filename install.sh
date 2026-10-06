#!/bin/sh
# The git route. Clone the repo into the plugins folder and run this from
# there:
#
#   git clone https://github.com/eastbluewizard-official/omarchy-stickies \
#       ~/.config/omarchy/plugins/eastbluewizard.stickies
#   ~/.config/omarchy/plugins/eastbluewizard.stickies/install.sh
#
# That folder is the plugin, the same git checkout `omarchy plugin add`
# makes: nothing is linked or copied anywhere else. This only adds the
# extras, through `stickies integrate --yes --install`: the `stickies`
# command in ~/.local/bin, the keybinding drop-in (hypr/stickies.lua, copied
# into ~/.local/state/omarchy/toggles/hypr, which Omarchy loads), and in the
# live session it enables the plugin, reloads Hyprland, publishes the hub
# card and restarts the shell if the plugin's QML changed since the last
# install. Hand-written configs are never edited. Updates: git pull, then
# this again. Undo: ./uninstall.sh.
#
# An install from before 1.3.0 symlinked a checkout into the plugins folder;
# run from that checkout, this replaces the symlink with a git clone of it
# (your notes stay where they are).
#
# Everything derives from $HOME, so a temp HOME is a full dry run (the live
# steps only run when HOME is your real home). STICKIES_PLUGINS (the
# plugins folder), STICKIES_BIN and STICKIES_HYPR_DIR override single
# targets; STICKIES_PLUGINS or STICKIES_NO_LIVE=1 also skip the live steps.
set -eu
here=$(cd "$(dirname "$0")" && pwd -P)
plugins="${STICKIES_PLUGINS:-$HOME/.config/omarchy/plugins}"
plugin="$plugins/eastbluewizard.stickies"

if [ ! -L "$plugin" ] && [ "$(cd "$plugin" 2>/dev/null && pwd -P)" != "$here" ]; then
    cat >&2 <<EOF
install.sh: this checkout is not the installed plugin ($here).
The plugin is the folder $plugin itself. Either:
  omarchy plugin add https://github.com/eastbluewizard-official/omarchy-stickies --enable
or clone it there and run its install.sh:
  git clone https://github.com/eastbluewizard-official/omarchy-stickies $plugin
  $plugin/install.sh
To run a development checkout: omarchy plugin add $here --enable (see CONTRIBUTING.md).
EOF
    exit 1
fi

"$here/stickies" integrate --yes --install
# Search by meaning needs a one-time download, which needs a yes: never here.
"$here/stickies" setup --status 2>/dev/null | grep -q "semantic search: on" \
    || echo "search by meaning: run \`stickies setup\` once (shows what it downloads, asks first)"
