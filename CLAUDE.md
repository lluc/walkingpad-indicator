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

## Log d'activité

Une session est clôturée dans deux cas :
- **Déconnexion BLE** (tapis éteint / coupure réseau)
- **Inactivité** : vitesse = 0 pendant `IDLE_TIMEOUT_S = 60` secondes

Si la distance ≥ 10 m (`MIN_LOG_DISTANCE_M`), une entrée JSONL est ajoutée dans :

```
~/.local/share/walkingpad-indicator/activity.log
```

Format d'une entrée :
```json
{"date": "2026-03-10", "start": "2026-03-10T09:15:00", "end": "2026-03-10T09:45:23", "duration_s": 1823, "distance_m": 2410, "steps": 3102, "max_speed_kmh": 4.5, "avg_speed_kmh": 3.82}
```

Les valeurs `distance_m` et `steps` sont des **deltas** calculés entre le snapshot initial (première notification FTMS) et les dernières valeurs reçues.

### Checkpoint WIP (résistance aux crashes)

Toutes les 60 secondes, l'état courant de la session est sauvegardé dans :

```
~/.local/share/walkingpad-indicator/session_current.json
```

Au démarrage, si ce fichier existe (crash ou SIGKILL précédent), la session est récupérée et écrite dans `activity.log`. Le fichier est supprimé après écriture réussie.

## Menu de l'indicateur

| Item | Type | Comportement |
|---|---|---|
| **Statistiques** | MenuItem | Ouvre une fenêtre GTK avec 3 graphiques matplotlib (30 derniers jours) |
| **Veille (pause connexion)** | CheckMenuItem | Active/désactive les tentatives de reconnexion BLE |
| *(séparateur)* | | |
| **Redémarrer** | MenuItem | Reconnexion BLE propre |
| *(séparateur)* | | |
| **Quitter** | MenuItem | Arrêt complet |

### Mode veille

`self._paused` est un `bool` partagé entre thread GTK (écriture) et thread BLE (lecture). La GIL Python garantit l'atomicité — aucun lock nécessaire.

- Veille activée → label `WP: veille`, aucun scan BLE
- Veille désactivée → reprise du scan au prochain tour de boucle (~1 s)
- La veille n'interrompt pas une connexion BLE déjà active

### Fenêtre de statistiques

Imports matplotlib **lazy** (dans `_on_show_stats`) pour ne pas alourdir le démarrage.
Backend : `GTK3Agg` (`matplotlib.use('GTK3Agg')`), canvas embarqué dans `Gtk.Box`.
Fenêtre 860×700 px, 3 subplots (`sharex=True`) : distance (km) / pas / durée (min) par jour.

## Dépendances

| Paquet | Source | Rôle |
|---|---|---|
| `python3-gi` | système | GTK3 + AppIndicator |
| `gir1.2-ayatanaappindicator3-0.1` | système | Barre de statut GNOME |
| `bleak` | pip (venv) | BLE async |
| `matplotlib` | pip (venv) | Graphiques dans la fenêtre Statistiques |
| Extension GNOME `ubuntu-appindicators@ubuntu.com` | système, activée | Affichage dans la barre |

## Autostart

[~/.config/autostart/walkingpad-indicator.desktop](~/.config/autostart/walkingpad-indicator.desktop) — délai de 5 s au démarrage pour laisser BlueZ s'initialiser.
