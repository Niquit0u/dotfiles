#!/usr/bin/env bash
# Regenera las listas de paquetes instalados explicitamente
cd "$(dirname "$0")"
pacman -Qqen > packages/pkglist.txt
pacman -Qqem > packages/aur.txt
echo "Listas actualizadas: $(wc -l < packages/pkglist.txt) oficiales, $(wc -l < packages/aur.txt) AUR"
