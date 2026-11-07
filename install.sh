#!/usr/bin/env bash
# Restaura el sistema despues de formatear (CachyOS / Arch).
# Uso: git clone https://github.com/<usuario>/dotfiles ~/dotfiles && cd ~/dotfiles && ./install.sh
set -euo pipefail
cd "$(dirname "$0")"
DOTFILES="$PWD"

PACKAGES=(bash cava easyeffects environment fastfetch fish git hypr kitty micro opencode qt serpantinum shelly systemd-user xdg zsh)

echo "==> Paquetes oficiales"
sudo pacman -Syu --needed --noconfirm git base-devel stow
# Ignora los paquetes que ya no existan en los repos
comm -12 <(sort packages/pkglist.txt) <(pacman -Slq | sort) | sudo pacman -S --needed --noconfirm -

echo "==> yay + paquetes AUR"
if ! command -v yay >/dev/null; then
  tmp=$(mktemp -d)
  git clone https://aur.archlinux.org/yay-bin.git "$tmp/yay-bin"
  (cd "$tmp/yay-bin" && makepkg -si --noconfirm)
  rm -rf "$tmp"
fi
yay -S --needed --noconfirm - < packages/aur.txt || echo "!! Algunos paquetes AUR fallaron, revisalos a mano"

echo "==> Enlazando configs con stow"
# --adopt absorbe archivos que ya existan (ej. el .bashrc por defecto) y crea los enlaces;
# git checkout luego restaura las versiones del repo encima de ellos.
stow --adopt -t "$HOME" "${PACKAGES[@]}"
git checkout -- .

echo "==> Archivos de sistema (fix del mouse para Piper)"
sudo install -m 755 system/ratbagd-mouse-watch.sh /usr/local/bin/ratbagd-mouse-watch.sh
sudo install -m 644 system/ratbagd-mouse-watch.service /etc/systemd/system/ratbagd-mouse-watch.service
sudo systemctl daemon-reload
sudo systemctl enable ratbagd-mouse-watch.service || true

echo "==> Servicios de usuario"
systemctl --user daemon-reload
systemctl --user enable opencode-ollama-adapter.service || true

echo "==> Shell por defecto: fish"
[ "$(getent passwd "$USER" | cut -d: -f7)" = "/usr/bin/fish" ] || chsh -s /usr/bin/fish

echo
echo "Listo. Pasos manuales pendientes (ver README.md):"
echo "  - Recrear ~/.config/opencode/service.json (contiene una contrasena, no esta en el repo)"
echo "  - Instalar Piper/libratbag a mano si los sigues usando"
echo "  - Reiniciar la sesion"
