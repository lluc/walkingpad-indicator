# WalkingPad GNOME Indicator

Affiche en temps réel les données d'un tapis de marche KingSmith/WalkingPad dans la barre de statut GNOME (Ubuntu 24.04+).

```
3.5km/h  1.23km  0:12:34  847stp
```

## Prérequis système

- Ubuntu 24.04 (ou dérivé GNOME avec AppIndicator3)
- `python3-gi`, `gir1.2-ayatanaappindicator3-0.1` (généralement préinstallés)
- Extension GNOME **Ubuntu AppIndicators** activée :
  ```bash
  gnome-extensions enable ubuntu-appindicators@ubuntu.com
  ```
- Bluetooth actif (`bluetoothctl show` doit afficher `Powered: yes`)

## Installation

### 1. Créer le virtualenv

```bash
python3 -m venv --system-site-packages ~/.local/share/walkingpad-venv
~/.local/share/walkingpad-venv/bin/pip install bleak matplotlib
```

> `--system-site-packages` est obligatoire pour accéder à `python3-gi` (GTK3).

### 2. Rendre le script exécutable

```bash
chmod +x run.sh
```

## Trouver l'adresse BLE du tapis

Allumez le tapis (sans télécommande connectée dessus), puis lancez un scan :

```bash
bluetoothctl scan on
```

Repérez la ligne avec un nom de type `KS-AP-ZD3` ou similaire et notez l'adresse MAC (`XX:XX:XX:XX:XX:XX`).

> Le tapis n'est visible en BLE que quand il est allumé et non déjà connecté à un autre appareil.

## Lancement

### Mode normal

```bash
./run.sh
```

L'indicateur apparaît dans la barre GNOME. Il scanne automatiquement le tapis au démarrage, puis se reconnecte en cas de coupure.

### Avec adresse fixe (évite le scan)

```bash
./run.sh --address XX:XX:XX:XX:XX:XX
```

L'adresse est mémorisée dans `~/.local/share/walkingpad-indicator/device_address.txt` et réutilisée aux lancements suivants.

### Mode debug

```bash
./run.sh --debug
```

Affiche les paquets BLE bruts (flags, hex, valeurs parsées) — utile pour diagnostiquer des problèmes de données.

## Lancement automatique au démarrage

Copiez le fichier `.desktop` dans le répertoire d'autostart GNOME :

```bash
cp walkingpad-indicator.desktop ~/.config/autostart/
```

Vérifiez que le chemin `Exec=` dans ce fichier pointe bien vers votre `run.sh` :

```ini
Exec=/chemin/absolu/vers/tapis_marche/run.sh
```

L'indicateur démarre automatiquement 5 secondes après la connexion de session.

## Menu de l'indicateur

Clic sur l'icône dans la barre → menu avec :
- **Statistiques** — ouvre une fenêtre avec l'historique d'activité sur 30 jours
- **Veille (pause connexion)** — suspend les tentatives de reconnexion BLE (utile quand le tapis est rangé)
- **Redémarrer** — reconnexion BLE propre (utile si le tapis a été mis en veille)
- **Quitter** — arrêt complet

## Fenêtre de statistiques

Cliquer sur **Statistiques** dans le menu affiche une fenêtre avec :
- Résumé : nombre de sessions, distance totale, pas totaux, durée cumulée
- 3 graphiques sur les 30 derniers jours : distance par jour (km), pas par jour, durée par jour (minutes)

Nécessite `matplotlib` installé dans le venv :

```bash
~/.local/share/walkingpad-venv/bin/pip install matplotlib
```

## Mode veille

Quand le tapis est éteint, l'application tente de se reconnecter en permanence (scan BLE toutes les 5 s). Pour suspendre ces tentatives :

1. Cliquer sur l'icône dans la barre
2. Activer **Veille (pause connexion)**

Le label passe à `WP: veille` et aucun scan n'est effectué. Pour reprendre, décocher l'option — le scan reprend en ~1 seconde.

> Note : la veille n'interrompt pas une connexion BLE déjà active.

## Log d'activité

Chaque session de marche est automatiquement enregistrée dans :

```
~/.local/share/walkingpad-indicator/activity.log
```

Format : une entrée JSON par ligne (JSONL), ajoutée à la déconnexion du tapis.

```json
{"date": "2026-03-10", "start": "2026-03-10T09:15:00", "end": "2026-03-10T09:45:23", "duration_s": 1823, "distance_m": 2410, "steps": 3102, "max_speed_kmh": 4.5, "avg_speed_kmh": 3.82}
```

Les sessions de moins de 10 m ne sont pas enregistrées (tapis allumé sans marche).

Une session se termine automatiquement si la vitesse reste à 0 pendant 60 secondes consécutives (détection d'inactivité), même si le tapis reste connecté en BLE.

### Résistance aux crashes

Toutes les 60 secondes, l'état courant de la session est sauvegardé dans `session_current.json`. Si l'application est tuée brutalement (`kill -9`, coupure de courant), la session est récupérée et écrite dans `activity.log` au prochain démarrage.

Exemples d'exploitation :

```bash
# Afficher toutes les sessions
cat ~/.local/share/walkingpad-indicator/activity.log | python3 -c "
import sys, json
for line in sys.stdin:
    s = json.loads(line)
    print(f\"{s['date']}  {s['start'][11:16]}–{s['end'][11:16]}  {s['distance_m']/1000:.2f}km  {s['steps']}stp  moy {s['avg_speed_kmh']}km/h\")
"

# Distance totale (jq)
jq -s '[.[].distance_m] | add / 1000' ~/.local/share/walkingpad-indicator/activity.log
```

## Protocole technique

Le tapis expose le service **FTMS** (Fitness Machine Service, UUID `0x1826`) standard Bluetooth. Les données sont reçues via des notifications sur la caractéristique **Treadmill Data** (`0x2ACD`) à environ 1 Hz.

Extension propriétaire KingSmith : le **bit 13** des flags FTMS contient le nombre de pas (uint16 little-endian), non documenté dans la spécification Bluetooth.

## Licence

MIT — voir [LICENSE](LICENSE).

## Dépannage

| Symptôme | Cause probable | Solution |
|---|---|---|
| Icône absente de la barre | Extension AppIndicators désactivée | `gnome-extensions enable ubuntu-appindicators@ubuntu.com` |
| `WP: scan...` en permanence | Tapis éteint ou télécommande connectée | Allumer le tapis sans télécommande |
| "Device not found" après redémarrage | Adresse privée/rotative en cache | Supprimer `~/.local/share/walkingpad-indicator/device_address.txt` |
| Pas (`stp`) toujours à 0 | Modèle ne supportant pas le bit 13 | Lancer avec `--debug` et vérifier les flags reçus |
