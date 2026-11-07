# dotfiles

Mi configuracion de CachyOS + Hyprland, gestionada con [GNU Stow](https://www.gnu.org/software/stow/).

## Restaurar despues de formatear

```bash
sudo pacman -S git
git clone https://github.com/<usuario>/dotfiles ~/dotfiles
cd ~/dotfiles
./install.sh
```

El script instala los paquetes (`packages/`), yay y los paquetes AUR, enlaza todas las configs con stow,
instala el fix del mouse para Piper (`system/`) y activa los servicios.

## Estructura

Cada carpeta es un "paquete" de stow que refleja la ruta desde `~`:

| Paquete | Contenido |
|---|---|
| hypr | `~/.config/hypr` |
| kitty, fish, fastfetch, cava, micro, easyeffects | sus carpetas en `~/.config` |
| qt | qt5ct y qt6ct |
| environment | `~/.config/environment.d` |
| serpantinum, shelly, opencode | configs de esas apps |
| systemd-user | servicio `opencode-ollama-adapter` |
| xdg | mimeapps.list, user-dirs |
| zsh, bash, git | `.zshrc`, `.bashrc`, `.bash_profile`, `.gitconfig` |
| system | archivos de `/usr/local/bin` y `/etc/systemd/system` (no se usan con stow, los copia `install.sh`) |
| packages | `pkglist.txt` (repos oficiales) y `aur.txt` |

## Uso diario

Las configs en `~/.config` son enlaces a este repo, asi que editarlas modifica el repo directamente:

```bash
cd ~/dotfiles
./update-packages.sh   # refresca las listas de paquetes
git add -A && git commit -m "update" && git push
```

Agregar una config nueva (ej. `waybar`):

```bash
mkdir -p ~/dotfiles/waybar/.config
mv ~/.config/waybar ~/dotfiles/waybar/.config/
cd ~/dotfiles && stow -t ~ waybar
```
Luego agregala a la lista `PACKAGES` de `install.sh`.
