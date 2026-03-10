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

    CACHE_FILE = Path.home() / ".local" / "share" / "walkingpad-indicator" / "device_address.txt"

    LABEL_DISCONNECTED = "WP: --"
    LABEL_SCANNING     = "WP: scan..."
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

        self._running   = True
        self._connected = False
        self._restart   = False  # True → relancer après arrêt propre

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

    def _on_quit(self, _source=None) -> None:
        logging.info("Arrêt demandé.")
        self._running = False
        if self.ble_loop and self.ble_loop.is_running():
            asyncio.run_coroutine_threadsafe(self._ble_shutdown(), self.ble_loop)
        if self.main_loop:
            self.main_loop.quit()

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
        while self._running:
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
        self._treadmill_data = {}

        def on_ftms_data(_sender, data: bytearray) -> None:
            parsed = parse_treadmill_data(data)
            if parsed:
                self._treadmill_data.update(parsed)
                GLib.idle_add(self._update_label)

        async with bleak.BleakClient(address) as client:
            self._ble_client = client
            self._connected  = True
            logging.info("Connecté !")
            self._save_cached_address(address)

            await client.start_notify(TREADMILL_DATA_UUID, on_ftms_data)
            logging.info("Notifications FTMS activées (vitesse, distance, durée, pas).")

            while self._running and client.is_connected:
                await asyncio.sleep(1.0)

            await client.stop_notify(TREADMILL_DATA_UUID)

        self._ble_client = None
        logging.info("Déconnecté de %s.", address)

    async def _ble_shutdown(self) -> None:
        if self._ble_client and self._connected:
            try:
                await self._ble_client.disconnect()
            except Exception:
                pass

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
