-- eastbluewizard.stickies keybindings. Copied here by the stickies install.sh
-- and removed by `stickies uninstall`; edit hypr/stickies.lua in the repo, not this copy.
-- Omarchy loads every *.lua in this folder after its defaults and the personal
-- ~/.config/hypr/bindings.lua; all keys were free in all of them (SUPER + ALT + K,
-- the first pick for All notes, is Omarchy's "Tmux keybindings").
-- Each key calls omarchy-shell without -q (which exits 0 on every failure);
-- only when that call fails does `stickies shell <method>` run: it tries once
-- more and writes the reason to ~/.local/state/stickies/stickies.log.
local function call(method)
  return "omarchy-shell stickies " .. method .. " >/dev/null 2>&1 || stickies shell " .. method
end
o.bind("SUPER + ALT + N", "New sticky note", call("newNote"))
o.bind("SUPER + ALT + SHIFT + N", "Stickies above windows (toggle)", call("front"))
o.bind("SUPER + ALT + V", "Sticky note from the clipboard", call("paste"))
o.bind("SUPER + ALT + J", "Find a sticky note", call("find"))
o.bind("SUPER + ALT + A", "Ask your sticky notes", call("chat"))
o.bind("SUPER + ALT + L", "Stickies layout: free / waterfall right / left", call("cycleLayout"))
o.bind("SUPER + ALT + W", "Collapse the stickies column (toggle)", call("collapse"))
o.bind("SUPER + ALT + O", "All sticky notes", call("list"))
