#!/bin/bash
# Reinicia ratbagd cuando el mouse Logitech se conecta y ratbagd no lo tiene
journalctl -k -f -n0 -o cat | while read -r line; do
  if [[ $line == *"logitech-hidpp-device"*"device connected"* ]]; then
    sleep 2
    if ratbagctl list 2>&1 | grep -q "No devices"; then
      systemctl restart ratbagd
    fi
  fi
done
