#!/usr/bin/env python3
"""
WalkingPad GNOME Status Bar Indicator
Affiche vitesse, distance et durée dans la barre de statut GNOME en temps réel.
Utilise le protocole FTMS (Fitness Machine Service) standard via BLE.

Usage:
    ~/.local/share/walkingpad-venv/bin/python walkingpad_indicator.py
    ~/.local/share/walkingpad-venv/bin/python walkingpad_indicator.py --debug
    ~/.local/share/walkingpad-venv/bin/python walkingpad_indicator.py --address XX:XX:XX:XX:XX:XX
"""

import asyncio
import datetime
import json
import logging
import os
import signal
import sys
import threading
from pathlib import Path
from typing import Optional

import bleak
import gi
gi.require_version('AyatanaAppIndicator3', '0.1')
gi.require_version('Gtk', '3.0')
from gi.repository import AyatanaAppIndicator3, GLib, Gtk

# UUID FTMS Treadmill Data (standard Bluetooth)
TREADMILL_DATA_UUID = "00002acd-0000-1000-8000-00805f9b34fb"


def parse_treadmill_data(data: bytearray) -> Optional[dict]:
    """
    Parse le format FTMS Treadmill Data (Bluetooth spec section 3.68).
    Retourne un dict avec les clés présentes : speed (km/h), distance (m), time (s).
    """
    if len(data) < 2:
        return None

    flags = int.from_bytes(data[0:2], 'little')
    offset = 2
    result = {}

    # Bit 0 = "More Data" : si 0, la vitesse instantanée est présente
    if not (flags & 0x0001):
        if offset + 2 <= len(data):
            result['speed'] = int.from_bytes(data[offset:offset+2], 'little') / 100.0
            offset += 2

    # Bit 1 : vitesse moyenne (on skip)
    if flags & 0x0002:
        offset += 2

    # Bit 2 : distance totale (uint24, mètres)
    if flags & 0x0004:
        if offset + 3 <= len(data):
            result['distance'] = int.from_bytes(data[offset:offset+3], 'little')
            offset += 3

    # Bit 3 : inclinaison + angle rampe (on skip, 4 octets)
    if flags & 0x0008:
        offset += 4

    # Bit 4 : gain d'élévation (on skip, 4 octets)
    if flags & 0x0010:
        offset += 4

    # Bit 5 : allure instantanée (on skip)
    if flags & 0x0020:
        offset += 2

    # Bit 6 : allure moyenne (on skip)
    if flags & 0x0040:
        offset += 2

    # Bit 7 : énergie dépensée (on skip, 5 octets)
    if flags & 0x0080:
        offset += 5

    # Bit 8 : fréquence cardiaque (on skip)
    if flags & 0x0100:
        offset += 1

    # Bit 9 : équivalent métabolique (on skip)
    if flags & 0x0200:
        offset += 1

    # Bit 10 : temps écoulé (uint16, secondes)
    if flags & 0x0400:
        if offset + 2 <= len(data):
            result['time'] = int.from_bytes(data[offset:offset+2], 'little')
            offset += 2

    # Bit 11 : temps restant (on skip, uint16)
    if flags & 0x0800:
        offset += 2

    # Bit 12 : force sur courroie + puissance (on skip, 4 octets)
    if flags & 0x1000:
        offset += 4

    # Bit 13 : nombre de pas (extension KingSmith non standard, uint16 little-endian)
    if flags & 0x2000:
        if offset + 2 <= len(data):
            result['steps'] = int.from_bytes(data[offset:offset+2], 'little')
            offset += 2

    logging.debug("FTMS raw: flags=0x%04X  hex=%s  parsed=%s", flags, data.hex(), result)
    return result



class WalkingPadIndicator:
    """
    GTK AppIndicator affichant les données BLE du WalkingPad dans la barre GNOME.

    Thread principal : GLib.MainLoop + AppIndicator3 (GTK, non thread-safe).
    Thread BLE       : asyncio event loop + bleak (FTMS direct).
    Bridge           : GLib.idle_add() (thread-safe, sens BLE → GTK).
    """

    DEVICE_NAME_PATTERNS = ["ks-", "walkingpad", "kingsmith"]
    DEVICE_NAME_EXCLUDES = ["remote", "controller", "telecommande"]
    RECONNECT_DELAY_S    = 5.0
    SCAN_TIMEOUT_S       = 8.0

    CACHE_FILE       = Path.home() / ".local" / "share" / "walkingpad-indicator" / "device_address.txt"
    LOG_FILE         = Path.home() / ".local" / "share" / "walkingpad-indicator" / "activity.log"
    SESSION_WIP_FILE = Path.home() / ".local" / "share" / "walkingpad-indicator" / "session_current.json"

    MIN_LOG_DISTANCE_M = 10   # ignorer les sessions < 10 m (tapis allumé mais pas de marche)
    IDLE_TIMEOUT_S     = 60   # secondes sans mouvement avant de clore la session

    LABEL_DISCONNECTED = "WP: --"
    LABEL_SCANNING     = "WP: scan..."
    LABEL_PAUSED       = "WP: veille"
    LABEL_GUIDE        = "WP: 9.9km/h  9.99km  9:99:99  9999stp"

    def __init__(self, debug: bool = False):
        self._debug = debug

        # --- état GTK (thread principal uniquement) ---
        self.indicator: Optional[AyatanaAppIndicator3.Indicator] = None
        self.menu:      Optional[Gtk.Menu]                       = None
        self.main_loop: Optional[GLib.MainLoop]                  = None

        # --- état BLE (thread BLE uniquement) ---
        self.ble_loop:    Optional[asyncio.AbstractEventLoop] = None
        self.ble_thread:  Optional[threading.Thread]          = None
        self._ble_client: Optional[bleak.BleakClient]         = None

        # --- données tapis (mises à jour depuis le thread BLE, lues par GTK via idle_add) ---
        self._treadmill_data: dict = {}   # speed, distance, time, steps

        # --- suivi de session (thread BLE uniquement) ---
        self._session_start:       Optional[datetime.datetime] = None
        self._session_data_start:  Optional[dict]              = None
        self._session_max_speed:   float                       = 0.0
        self._session_speed_sum:   float                       = 0.0
        self._session_speed_count: int                         = 0

        self._running   = True
        self._connected = False
        self._restart   = False  # True → relancer après arrêt propre
        self._paused    = False  # True → pas de scan/reconnexion BLE

        # --- fenêtre de statistiques (thread GTK uniquement) ---
        self._stats_window: Optional[Gtk.Window] = None

    # ------------------------------------------------------------------
    # Thread principal — GTK
    # ------------------------------------------------------------------

    def _build_indicator(self) -> None:
        self.indicator = AyatanaAppIndicator3.Indicator.new(
            "walkingpad-indicator",
            "media-playback-start",
            AyatanaAppIndicator3.IndicatorCategory.APPLICATION_STATUS,
        )
        self.indicator.set_status(AyatanaAppIndicator3.IndicatorStatus.ACTIVE)
        self.indicator.set_label(self.LABEL_DISCONNECTED, self.LABEL_GUIDE)

        self.menu = Gtk.Menu()

        item_stats = Gtk.MenuItem(label="Statistiques")
        item_stats.connect("activate", self._on_show_stats)
        self.menu.append(item_stats)

        self._item_pause = Gtk.CheckMenuItem(label="Veille (pause connexion)")
        self._item_pause.connect("toggled", self._on_toggle_pause)
        self.menu.append(self._item_pause)

        self.menu.append(Gtk.SeparatorMenuItem())

        item_restart = Gtk.MenuItem(label="Redémarrer")
        item_restart.connect("activate", self._on_restart)
        self.menu.append(item_restart)

        self.menu.append(Gtk.SeparatorMenuItem())

        item_quit = Gtk.MenuItem(label="Quitter")
        item_quit.connect("activate", self._on_quit)
        self.menu.append(item_quit)

        self.menu.show_all()
        self.indicator.set_menu(self.menu)

    def _update_label(self) -> bool:
        """Appelé via GLib.idle_add() depuis le thread BLE. Retourne False (one-shot)."""
        data     = self._treadmill_data
        speed    = data.get('speed', 0.0)
        distance = data.get('distance', 0) / 1000.0  # m → km
        elapsed  = int(data.get('time', 0))
        steps    = data.get('steps', 0)

        h = elapsed // 3600
        m = (elapsed % 3600) // 60
        s = elapsed % 60

        label = f"{speed:.1f}km/h  {distance:.2f}km  {h}:{m:02d}:{s:02d}  {steps}stp"
        self.indicator.set_label(label, self.LABEL_GUIDE)
        return False

    def _set_label_safe(self, label: str) -> None:
        GLib.idle_add(self.indicator.set_label, label, self.LABEL_GUIDE)

    def _on_restart(self, _source=None) -> None:
        """Demande un redémarrage — l'execv se fait après le nettoyage BLE complet."""
        logging.info("Redémarrage demandé…")
        self._restart = True
        self._on_quit()

    def _on_toggle_pause(self, item: Gtk.CheckMenuItem) -> None:
        self._paused = item.get_active()
        if self._paused and not self._connected:
            self._set_label_safe(self.LABEL_PAUSED)
        elif not self._paused and not self._connected:
            self._set_label_safe(self.LABEL_DISCONNECTED)
        logging.info("Veille %s.", "activée" if self._paused else "désactivée")

    def _on_quit(self, _source=None) -> None:
        logging.info("Arrêt demandé.")
        self._running = False
        if self.ble_loop and self.ble_loop.is_running():
            asyncio.run_coroutine_threadsafe(self._ble_shutdown(), self.ble_loop)
        if self.main_loop:
            self.main_loop.quit()

    def _load_sessions(self) -> list:
        """Lit activity.log et retourne la liste des sessions valides."""
        sessions = []
        if not self.LOG_FILE.exists():
            return sessions
        try:
            for line in self.LOG_FILE.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    try:
                        sessions.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        except Exception as exc:
            logging.warning("Lecture activity.log : %s", exc)
        return sessions

    def _on_show_stats(self, _source=None) -> None:
        """Ouvre (ou met au premier plan) la fenêtre de statistiques."""
        if self._stats_window and self._stats_window.get_visible():
            self._stats_window.present()
            return

        import matplotlib
        matplotlib.use('GTK3Agg')
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_gtk3agg import FigureCanvasGTK3Agg as FigureCanvas

        sessions = self._load_sessions()

        win = Gtk.Window(title="WalkingPad — Statistiques (30 derniers jours)")
        win.set_default_size(860, 700)
        win.connect("destroy", lambda w: setattr(self, '_stats_window', None))
        self._stats_window = win

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        vbox.set_margin_top(8)
        vbox.set_margin_bottom(8)
        vbox.set_margin_start(8)
        vbox.set_margin_end(8)
        win.add(vbox)

        if not sessions:
            label = Gtk.Label(label="Aucune session enregistrée pour l'instant.")
            vbox.pack_start(label, True, True, 0)
            win.show_all()
            return

        # Filtrer les 30 derniers jours
        today      = datetime.date.today()
        cutoff     = today - datetime.timedelta(days=29)
        recent     = [s for s in sessions if s.get('date', '') >= str(cutoff)]
        all_days   = sorted({s['date'] for s in recent})

        # Agréger par jour
        daily_dist = {d: 0.0 for d in all_days}
        daily_stp  = {d: 0   for d in all_days}
        daily_dur  = {d: 0.0 for d in all_days}
        for s in recent:
            d = s['date']
            daily_dist[d] += s.get('distance_m', 0) / 1000.0
            daily_stp[d]  += s.get('steps', 0)
            daily_dur[d]  += s.get('duration_s', 0) / 60.0

        x      = list(range(len(all_days)))
        labels = [datetime.date.fromisoformat(d).strftime('%d/%m') for d in all_days]

        # Résumé texte
        tot_km  = sum(s.get('distance_m', 0) for s in sessions) / 1000.0
        tot_stp = sum(s.get('steps', 0)      for s in sessions)
        tot_s   = sum(s.get('duration_s', 0) for s in sessions)
        tot_h, tot_m = divmod(tot_s // 60, 60)
        summary = (f"{len(sessions)} sessions  ·  {tot_km:.1f} km total  ·  "
                   f"{tot_stp:,} pas  ·  {int(tot_h)}h{int(tot_m):02d}min")
        lbl = Gtk.Label(label=summary)
        lbl.set_halign(Gtk.Align.START)
        vbox.pack_start(lbl, False, False, 0)

        # Figure matplotlib
        fig = Figure(figsize=(9, 7), tight_layout=True)
        color = '#4c9be8'

        ax1 = fig.add_subplot(3, 1, 1)
        ax1.bar(x, [daily_dist[d] for d in all_days], color=color)
        ax1.set_ylabel('km')
        ax1.set_title('Distance par jour')
        ax1.set_xticks(x)
        ax1.set_xticklabels([])

        ax2 = fig.add_subplot(3, 1, 2, sharex=ax1)
        ax2.bar(x, [daily_stp[d] for d in all_days], color='#6cc86c')
        ax2.set_ylabel('pas')
        ax2.set_title('Pas par jour')
        ax2.set_xticklabels([])

        ax3 = fig.add_subplot(3, 1, 3, sharex=ax1)
        ax3.bar(x, [daily_dur[d] for d in all_days], color='#e88c4c')
        ax3.set_ylabel('min')
        ax3.set_title('Durée par jour')
        ax3.set_xticks(x)
        ax3.set_xticklabels(labels, rotation=45, ha='right', fontsize=8)

        canvas = FigureCanvas(fig)
        canvas.set_size_request(840, 600)
        vbox.pack_start(canvas, True, True, 0)

        win.show_all()

    def run(self) -> None:
        self._build_indicator()
        self._start_ble_thread()
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, self._on_quit)
        logging.info("Interface prête. En attente du WalkingPad…")
        self.main_loop = GLib.MainLoop()
        self.main_loop.run()
        if self.ble_thread and self.ble_thread.is_alive():
            self.ble_thread.join(timeout=5.0)
        logging.info("Arrêt terminé.")

        if self._restart:
            logging.info("Relancement…")
            os.execv(sys.executable, [sys.executable] + sys.argv)

    # ------------------------------------------------------------------
    # Thread BLE
    # ------------------------------------------------------------------

    def _start_ble_thread(self) -> None:
        self.ble_loop   = asyncio.new_event_loop()
        self.ble_thread = threading.Thread(
            target=self._ble_thread_main, name="ble-thread", daemon=True,
        )
        self.ble_thread.start()

    def _ble_thread_main(self) -> None:
        asyncio.set_event_loop(self.ble_loop)
        try:
            self.ble_loop.run_until_complete(self._ble_main())
        finally:
            self.ble_loop.close()

    async def _ble_main(self) -> None:
        self._recover_wip_session()
        while self._running:
            if self._paused:
                self._set_label_safe(self.LABEL_PAUSED)
                await asyncio.sleep(1.0)
                continue
            self._set_label_safe(self.LABEL_SCANNING)

            address = self._load_cached_address()
            if address:
                logging.info("Connexion directe à l'adresse en cache : %s", address)
            else:
                address = await self._scan_for_device()

            if address is None:
                logging.info("Aucun WalkingPad trouvé. Tapis allumé ?")
                self._set_label_safe(self.LABEL_DISCONNECTED)
                await asyncio.sleep(self.RECONNECT_DELAY_S)
                continue

            try:
                await self._connect_and_listen(address)
            except Exception as exc:
                logging.warning("Erreur BLE (%s) – reconnexion dans %ss…", exc, self.RECONNECT_DELAY_S)
                self._save_cached_address("")  # forcer un scan au prochain tour
            finally:
                self._connected = False
                if self._running:
                    self._set_label_safe(self.LABEL_DISCONNECTED)
                    await asyncio.sleep(self.RECONNECT_DELAY_S)

    async def _connect_and_listen(self, address: str) -> None:
        """Connexion BLE : FTMS (vitesse/distance/temps) + KingSmith custom (pas)."""
        logging.info("Connexion à %s…", address)
        self._treadmill_data      = {}
        self._session_start       = datetime.datetime.now()
        self._session_data_start  = None
        self._session_max_speed   = 0.0
        self._session_speed_sum   = 0.0
        self._session_speed_count = 0

        def on_ftms_data(_sender, data: bytearray) -> None:
            parsed = parse_treadmill_data(data)
            if parsed:
                self._treadmill_data.update(parsed)
                # Capture le snapshot initial dès que la distance est disponible
                if self._session_data_start is None and 'distance' in parsed:
                    self._session_data_start = dict(self._treadmill_data)
                speed = parsed.get('speed', 0.0)
                if speed > 0:
                    self._session_max_speed    = max(self._session_max_speed, speed)
                    self._session_speed_sum   += speed
                    self._session_speed_count += 1
                GLib.idle_add(self._update_label)

        try:
            async with bleak.BleakClient(address) as client:
                self._ble_client = client
                self._connected  = True
                logging.info("Connecté !")
                self._save_cached_address(address)

                await client.start_notify(TREADMILL_DATA_UUID, on_ftms_data)
                logging.info("Notifications FTMS activées (vitesse, distance, durée, pas).")

                _loop_count  = 0
                _last_active = datetime.datetime.now()
                while self._running and client.is_connected:
                    await asyncio.sleep(1.0)
                    _loop_count += 1

                    if self._treadmill_data.get('speed', 0) > 0:
                        _last_active = datetime.datetime.now()

                    if _loop_count % 60 == 0:
                        self._write_session_wip()

                    # Clore la session si inactivité prolongée (tapis arrêté mais BLE connecté)
                    idle_s = (datetime.datetime.now() - _last_active).total_seconds()
                    if (self._session_speed_count > 0
                            and self._treadmill_data.get('speed', 0) == 0
                            and idle_s >= self.IDLE_TIMEOUT_S):
                        logging.info("Inactivité %ds — session close.", int(idle_s))
                        self._log_session()
                        self._reset_session()
                        _last_active = datetime.datetime.now()

                await client.stop_notify(TREADMILL_DATA_UUID)
        finally:
            self._ble_client = None
            logging.info("Déconnecté de %s.", address)
            self._log_session()

    async def _ble_shutdown(self) -> None:
        if self._ble_client and self._connected:
            try:
                await self._ble_client.disconnect()
            except Exception:
                pass

    def _log_session(self) -> None:
        """Écrit une entrée JSONL dans activity.log à la fin de chaque connexion BLE."""
        if not self._session_start:
            logging.info("_log_session: session_start absent, rien à enregistrer.")
            return
        if not self._session_data_start:
            logging.info("_log_session: aucune notification FTMS reçue, rien à enregistrer.")
            return

        data_end   = self._treadmill_data
        data_start = self._session_data_start

        distance_delta = data_end.get('distance', 0) - data_start.get('distance', 0)
        steps_delta    = data_end.get('steps',    0) - data_start.get('steps',    0)

        logging.info("_log_session: distance_delta=%dm  steps_delta=%d", distance_delta, steps_delta)

        if distance_delta < self.MIN_LOG_DISTANCE_M:
            logging.info("_log_session: distance trop faible (%dm < %dm), session ignorée.",
                         distance_delta, self.MIN_LOG_DISTANCE_M)
            self.SESSION_WIP_FILE.unlink(missing_ok=True)
            return

        end_time   = datetime.datetime.now()
        duration_s = int((end_time - self._session_start).total_seconds())
        avg_speed  = (
            round(self._session_speed_sum / self._session_speed_count, 2)
            if self._session_speed_count else 0.0
        )

        entry = {
            "date":          self._session_start.strftime("%Y-%m-%d"),
            "start":         self._session_start.strftime("%Y-%m-%dT%H:%M:%S"),
            "end":           end_time.strftime("%Y-%m-%dT%H:%M:%S"),
            "duration_s":    duration_s,
            "distance_m":    max(0, distance_delta),
            "steps":         max(0, steps_delta),
            "max_speed_kmh": round(self._session_max_speed, 1),
            "avg_speed_kmh": avg_speed,
        }

        try:
            self.LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with self.LOG_FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
            logging.info(
                "Session enregistrée : %.2fkm  %d stp  vitesse moy %.1fkm/h  [%s → %s]",
                distance_delta / 1000, max(0, steps_delta), avg_speed,
                entry["start"][11:], entry["end"][11:],
            )
            self.SESSION_WIP_FILE.unlink(missing_ok=True)
        except Exception as exc:
            logging.warning("Impossible d'écrire le log d'activité : %s", exc)

    def _reset_session(self) -> None:
        """Réinitialise les données de session sans déconnecter le BLE."""
        self._session_start       = datetime.datetime.now()
        self._session_data_start  = None
        self._session_max_speed   = 0.0
        self._session_speed_sum   = 0.0
        self._session_speed_count = 0

    def _write_session_wip(self) -> None:
        """Checkpoint toutes les 60 s : sauvegarde l'état courant dans session_current.json."""
        if not self._session_start or not self._session_data_start:
            return
        wip = {
            "session_start": self._session_start.isoformat(),
            "data_start":    self._session_data_start,
            "data_current":  dict(self._treadmill_data),
            "max_speed":     self._session_max_speed,
            "speed_sum":     self._session_speed_sum,
            "speed_count":   self._session_speed_count,
            "last_update":   datetime.datetime.now().isoformat(),
        }
        try:
            self.SESSION_WIP_FILE.parent.mkdir(parents=True, exist_ok=True)
            self.SESSION_WIP_FILE.write_text(json.dumps(wip), encoding="utf-8")
            logging.debug("Checkpoint session sauvegardé.")
        except Exception as exc:
            logging.warning("Checkpoint session impossible : %s", exc)

    def _recover_wip_session(self) -> None:
        """Au démarrage, finalise dans activity.log toute session interrompue (crash, SIGKILL)."""
        if not self.SESSION_WIP_FILE.exists():
            return
        logging.info("Session précédente non terminée détectée — récupération…")
        try:
            wip = json.loads(self.SESSION_WIP_FILE.read_text(encoding="utf-8"))

            session_start  = datetime.datetime.fromisoformat(wip["session_start"])
            data_start     = wip["data_start"]
            data_end       = wip["data_current"]
            max_speed      = wip.get("max_speed", 0.0)
            speed_sum      = wip.get("speed_sum", 0.0)
            speed_count    = wip.get("speed_count", 0)

            distance_delta = data_end.get('distance', 0) - data_start.get('distance', 0)
            steps_delta    = data_end.get('steps',    0) - data_start.get('steps',    0)

            if distance_delta < self.MIN_LOG_DISTANCE_M:
                logging.info("Session récupérée trop courte (%dm), ignorée.", distance_delta)
                self.SESSION_WIP_FILE.unlink(missing_ok=True)
                return

            end_time   = datetime.datetime.fromisoformat(wip.get("last_update", datetime.datetime.now().isoformat()))
            duration_s = int((end_time - session_start).total_seconds())
            avg_speed  = round(speed_sum / speed_count, 2) if speed_count else 0.0

            entry = {
                "date":          session_start.strftime("%Y-%m-%d"),
                "start":         session_start.strftime("%Y-%m-%dT%H:%M:%S"),
                "end":           end_time.strftime("%Y-%m-%dT%H:%M:%S"),
                "duration_s":    duration_s,
                "distance_m":    max(0, distance_delta),
                "steps":         max(0, steps_delta),
                "max_speed_kmh": round(max_speed, 1),
                "avg_speed_kmh": avg_speed,
                "recovered":     True,
            }

            self.LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with self.LOG_FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
            logging.info(
                "Session récupérée et enregistrée : %.2fkm  %d stp  [%s → %s]",
                distance_delta / 1000, max(0, steps_delta),
                entry["start"][11:], entry["end"][11:],
            )
            self.SESSION_WIP_FILE.unlink(missing_ok=True)
        except Exception as exc:
            logging.warning("Récupération session WIP échouée : %s", exc)

    # ------------------------------------------------------------------
    # Scan / cache
    # ------------------------------------------------------------------

    def _load_cached_address(self) -> Optional[str]:
        try:
            if self.CACHE_FILE.exists():
                addr = self.CACHE_FILE.read_text().strip()
                if addr:
                    return addr
        except Exception:
            pass
        return None

    def _save_cached_address(self, address: str) -> None:
        try:
            self.CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            self.CACHE_FILE.write_text(address)
        except Exception as exc:
            logging.warning("Impossible de sauvegarder l'adresse : %s", exc)

    def _matches_device_name(self, name: Optional[str]) -> bool:
        if not name:
            return False
        name_lower = name.lower()
        if any(exc in name_lower for exc in self.DEVICE_NAME_EXCLUDES):
            return False
        return any(pat in name_lower for pat in self.DEVICE_NAME_PATTERNS)

    async def _scan_for_device(self) -> Optional[str]:
        logging.info("Scan BLE en cours (timeout=%ss)…", self.SCAN_TIMEOUT_S)
        try:
            devices = await bleak.BleakScanner.discover(timeout=self.SCAN_TIMEOUT_S)
            for dev in devices:
                logging.info("  Appareil : %s  %s", dev.address, dev.name)
                if self._matches_device_name(dev.name):
                    logging.info("WalkingPad trouvé : %s (%s)", dev.name, dev.address)
                    self._save_cached_address(dev.address)
                    return dev.address
        except Exception as exc:
            logging.warning("Erreur scan : %s", exc)
        return None


# ----------------------------------------------------------------------
# Point d'entrée
# ----------------------------------------------------------------------

def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="WalkingPad GNOME Status Bar Indicator")
    parser.add_argument("--debug", action="store_true", help="Logs détaillés")
    parser.add_argument(
        "--address", metavar="MAC",
        help="Adresse BLE du tapis (ex: XX:XX:XX:XX:XX:XX). Évite le scan.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    app = WalkingPadIndicator(debug=args.debug)

    if args.address:
        app._save_cached_address(args.address)
        logging.info("Adresse fixe configurée : %s", args.address)

    app.run()


if __name__ == "__main__":
    main()
