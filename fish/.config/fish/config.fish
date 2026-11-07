source /usr/share/cachyos-fish-config/cachyos-config.fish

# overwrite greeting
# potentially disabling fastfetch
#function fish_greeting
#    # smth smth
#end

set -gx EDITOR micro  # o nano / code
set -gx VISUAL micro

# opencode
fish_add_path /home/nico/.opencode/bin
