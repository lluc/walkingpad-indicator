# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the application

```bash
# Lancement normal (adresse en cache ou scan automatique)
./run.sh

# Avec adresse BLE fixe (recommandé, évite le scan)
./run.sh --address XX:XX:XX:XX:XX:XX

# Avec logs détaillés (données FTMS brutes)
./run.sh --debug
```

## Python environment

Le projet utilise un venv avec accès aux paquets système (`--system-site-packages`) car `python3-gi` (GTK) ne peut pas être installé via pip.

```bash
# Venv
~/.local/share/walkingpad-venv/bin/python

# Vérifier la syntaxe
~/.local/share/walkingpad-venv/bin/python -m py_compile walkingpad_indicator.py
```

## Architecture

Un seul fichier : [walkingpad_indicator.py](walkingpad_indicator.py)

**Deux threads :**
- **Thread principal** : `GLib.MainLoop` + `AyatanaAppIndicator3` (barre GNOME). Toute modification GTK doit se faire ici.
- **Thread BLE** : `asyncio.new_event_loop()` + `bleak`. Communique vers GTK exclusivement via `GLib.idle_add()`.

**Flux de données :**
```
Tapis BLE → bleak notify → on_ftms_data() → GLib.idle_add(_update_label) → barre GNOME
```

## Protocole BLE

- **Device** : KingSmith KS-ZD3, nom BLE `KS-AP-ZD3`, adresse `XX:XX:XX:XX:XX:XX`
- **Protocole** : FTMS standard (UUID `0x1826`), caractéristique Treadmill Data `0x2ACD`
- **Données** : push automatique ~1/s, pas de polling nécessaire
- **Flags FTMS reçus** : `0x2484` — vitesse (bit 0), distance (bit 2), énergie (bit 7), temps (bit 10), **pas (bit 13, extension KingSmith non standard)**
- L'adresse est mise en cache dans `~/.local/share/walkingpad-indicator/device_address.txt`
- La télécommande `KS-REMOTE-01` occupe parfois le BLE ; si le tapis n'est pas visible, relancer après que la connexion remote/tapis est établie

## Dépendances

| Paquet | Source | Rôle |
|---|---|---|
| `python3-gi` | système | GTK3 + AppIndicator |
| `gir1.2-ayatanaappindicator3-0.1` | système | Barre de statut GNOME |
| `bleak` | pip (venv) | BLE async |
| Extension GNOME `ubuntu-appindicators@ubuntu.com` | système, activée | Affichage dans la barre |

## Autostart

[~/.config/autostart/walkingpad-indicator.desktop](~/.config/autostart/walkingpad-indicator.desktop) — délai de 5 s au démarrage pour laisser BlueZ s'initialiser.
