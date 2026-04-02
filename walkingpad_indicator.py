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
import http.server
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional

import bleak
import gi
gi.require_version('AyatanaAppIndicator3', '0.1')
gi.require_version('Gtk', '3.0')
from gi.repository import AyatanaAppIndicator3, Gdk, GLib, Gtk

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


class HikingVideoWindow:
    """
    Fenêtre plein écran jouant une vidéo de randonnée avec les données du tapis
    superposées en haut. La vitesse de lecture s'adapte à la vitesse du tapis
    (référence : REFERENCE_SPEED_KMH km/h → taux 1.0).

    Créée et utilisée exclusivement depuis le thread GTK principal.
    GStreamer (playbin + gtksink) est importé en lazy au premier usage.
    """

    REFERENCE_SPEED_KMH = 2.0

    def __init__(self, video_path: str, on_close_cb) -> None:
        gi.require_version('Gst', '1.0')
        from gi.repository import Gst
        self._Gst = Gst
        Gst.init(None)

        self._on_close_cb = on_close_cb
        self._current_rate = 1.0   # 0.0 = en pause par le tapis
        self._is_fullscreen = False

        # Pipeline GStreamer
        self._pipeline = Gst.ElementFactory.make("playbin", "playbin")
        gtksink = Gst.ElementFactory.make("gtksink", "gtksink")
        if not self._pipeline or not gtksink:
            raise RuntimeError("GStreamer playbin ou gtksink non disponible.")
        self._pipeline.set_property("video-sink", gtksink)
        self._pipeline.set_property("uri", Gst.filename_to_uri(video_path))

        # Fenêtre GTK (non plein écran au départ — déplacer puis F11)
        self._window = Gtk.Window()
        self._window.set_title(f"Randonnée — {Path(video_path).stem}  [F11 = plein écran · Échap = fermer]")
        self._window.set_default_size(1280, 720)
        self._window.connect("key-press-event", self._on_key_press)
        self._window.connect("destroy", self._on_destroy)

        # Overlay : widget vidéo en fond, barre d'info en haut
        overlay_container = Gtk.Overlay()

        video_widget = gtksink.props.widget
        video_widget.set_hexpand(True)
        video_widget.set_vexpand(True)
        overlay_container.add(video_widget)

        self._info_label = Gtk.Label(label="WalkingPad…")
        self._info_label.set_halign(Gtk.Align.CENTER)
        self._info_label.set_valign(Gtk.Align.START)
        css = b"""
            label {
                background-color: rgba(0, 0, 0, 0.65);
                color: white;
                font-size: 18px;
                font-family: monospace;
                padding: 8px 16px;
            }
        """
        provider = Gtk.CssProvider()
        provider.load_from_data(css)
        self._info_label.get_style_context().add_provider(
            provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        overlay_container.add_overlay(self._info_label)

        # Bouton plein écran (coin supérieur droit)
        self._fs_button = Gtk.Button()
        self._fs_button.set_image(
            Gtk.Image.new_from_icon_name("view-fullscreen", Gtk.IconSize.LARGE_TOOLBAR)
        )
        self._fs_button.set_halign(Gtk.Align.END)
        self._fs_button.set_valign(Gtk.Align.START)
        self._fs_button.set_tooltip_text("Plein écran (F11)")
        self._fs_button.connect("clicked", lambda _: self._toggle_fullscreen())
        btn_css = b"""
            button {
                background-color: rgba(0, 0, 0, 0.55);
                border: none;
                border-radius: 4px;
                padding: 4px;
                margin: 6px;
            }
            button:hover {
                background-color: rgba(0, 0, 0, 0.85);
            }
            button image {
                color: white;
            }
        """
        btn_provider = Gtk.CssProvider()
        btn_provider.load_from_data(btn_css)
        self._fs_button.get_style_context().add_provider(
            btn_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        overlay_container.add_overlay(self._fs_button)

        # Jauge temporelle discrète en bas
        self._progress_bar = Gtk.ProgressBar()
        self._progress_bar.set_halign(Gtk.Align.FILL)
        self._progress_bar.set_valign(Gtk.Align.END)
        self._progress_bar.set_fraction(0.0)
        pb_css = b"""
            progressbar trough {
                background-color: rgba(255, 255, 255, 0.15);
                min-height: 4px;
                border-radius: 0;
            }
            progressbar progress {
                background-color: rgba(255, 255, 255, 0.65);
                min-height: 4px;
                border-radius: 0;
            }
            progressbar {
                padding: 0;
                margin: 0;
            }
        """
        pb_provider = Gtk.CssProvider()
        pb_provider.load_from_data(pb_css)
        self._progress_bar.get_style_context().add_provider(
            pb_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        overlay_container.add_overlay(self._progress_bar)

        # Label de temps affiché brièvement lors des sauts temporels
        self._seek_label = Gtk.Label(label="")
        self._seek_label.set_halign(Gtk.Align.START)
        self._seek_label.set_valign(Gtk.Align.END)
        self._seek_label.set_no_show_all(True)
        sk_css = b"""
            label {
                background-color: rgba(0, 0, 0, 0.70);
                color: white;
                font-size: 13px;
                font-family: monospace;
                padding: 3px 8px;
                border-radius: 4px;
                margin-bottom: 10px;
            }
        """
        sk_provider = Gtk.CssProvider()
        sk_provider.load_from_data(sk_css)
        self._seek_label.get_style_context().add_provider(
            sk_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        overlay_container.add_overlay(self._seek_label)
        self._seek_hide_timer = None

        self._window.add(overlay_container)
        self._window.show_all()

        self._progress_timer = GLib.timeout_add(500, self._update_progress)

        # Bus GStreamer : loop sur EOS, log des erreurs
        bus = self._pipeline.get_bus()
        bus.add_watch(GLib.PRIORITY_DEFAULT, self._on_bus_message)

        self._pipeline.set_state(Gst.State.PLAYING)

    def update_treadmill_info(self, data: dict) -> None:
        """Met à jour l'overlay et adapte la vitesse de lecture. Thread GTK uniquement."""
        speed    = data.get('speed', 0.0)
        distance = data.get('distance', 0) / 1000.0
        elapsed  = int(data.get('time', 0))
        steps    = data.get('steps', 0)

        h = elapsed // 3600
        m = (elapsed % 3600) // 60
        s = elapsed % 60
        time_str = f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
        self._info_label.set_text(
            f"  {speed:.1f} km/h   {distance:.2f} km   {time_str}   {steps} pas  "
        )

        if speed == 0.0:
            if self._current_rate != 0.0:
                self._pipeline.set_state(self._Gst.State.PAUSED)
                self._current_rate = 0.0
        else:
            new_rate = speed / self.REFERENCE_SPEED_KMH
            if abs(new_rate - self._current_rate) > 0.05:
                if self._current_rate == 0.0:
                    self._pipeline.set_state(self._Gst.State.PLAYING)
                self._set_playback_rate(new_rate)
                self._current_rate = new_rate

    def _set_playback_rate(self, rate: float) -> None:
        Gst = self._Gst
        ok, pos = self._pipeline.query_position(Gst.Format.TIME)
        if not ok:
            pos = 0
        self._pipeline.seek(
            rate,
            Gst.Format.TIME,
            Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE,
            Gst.SeekType.SET, pos,
            Gst.SeekType.NONE, 0,
        )

    def _on_bus_message(self, bus, message) -> bool:
        Gst = self._Gst
        if message.type == Gst.MessageType.EOS:
            # Boucle : retour au début
            self._pipeline.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH, 0)
        elif message.type == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            logging.warning("GStreamer erreur : %s — %s", err, debug)
        return True

    def _update_progress(self) -> bool:
        """Rafraîchit la jauge temporelle toutes les 500 ms. Retourne True pour se répéter."""
        Gst = self._Gst
        ok_pos, pos = self._pipeline.query_position(Gst.Format.TIME)
        ok_dur, dur = self._pipeline.query_duration(Gst.Format.TIME)
        if ok_pos and ok_dur and dur > 0:
            self._progress_bar.set_fraction(min(1.0, pos / dur))
        return True

    def _seek_relative(self, offset_s: int) -> None:
        """Saute de offset_s secondes (positif = avance, négatif = recule)."""
        Gst = self._Gst
        ok_pos, pos = self._pipeline.query_position(Gst.Format.TIME)
        ok_dur, dur = self._pipeline.query_duration(Gst.Format.TIME)
        if not ok_pos:
            return
        new_pos = pos + offset_s * Gst.SECOND
        new_pos = max(0, new_pos)
        if ok_dur and dur > 0:
            new_pos = min(new_pos, dur)
        self._pipeline.seek(
            self._current_rate if self._current_rate != 0.0 else 1.0,
            Gst.Format.TIME,
            Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE,
            Gst.SeekType.SET, new_pos,
            Gst.SeekType.NONE, 0,
        )
        fraction = new_pos / dur if (ok_dur and dur > 0) else 0.0
        self._show_seek_time(new_pos, fraction)

    def _show_seek_time(self, pos_ns: int, fraction: float) -> None:
        """Affiche brièvement le temps courant au-dessus du curseur de la jauge."""
        Gst = self._Gst
        total_s = pos_ns // Gst.SECOND
        h = total_s // 3600
        m = (total_s % 3600) // 60
        s = total_s % 60
        time_str = f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
        self._seek_label.set_text(time_str)

        # Positionner le label horizontalement au-dessus du curseur
        win_width = self._window.get_allocated_width()
        lbl_width, _ = self._seek_label.get_preferred_width()
        x = int(fraction * win_width) - lbl_width // 2
        x = max(0, min(win_width - lbl_width, x))
        self._seek_label.set_margin_start(x)

        self._seek_label.show()

        # Annuler le timer précédent et en lancer un nouveau
        if self._seek_hide_timer:
            GLib.source_remove(self._seek_hide_timer)
        self._seek_hide_timer = GLib.timeout_add(2000, self._hide_seek_time)

    def _hide_seek_time(self) -> bool:
        self._seek_label.hide()
        self._seek_hide_timer = None
        return False

    def _toggle_fullscreen(self) -> None:
        if self._is_fullscreen:
            self._window.unfullscreen()
            self._is_fullscreen = False
            self._fs_button.set_image(
                Gtk.Image.new_from_icon_name("view-fullscreen", Gtk.IconSize.LARGE_TOOLBAR)
            )
            self._fs_button.set_tooltip_text("Plein écran (F11)")
        else:
            self._window.fullscreen()
            self._is_fullscreen = True
            self._fs_button.set_image(
                Gtk.Image.new_from_icon_name("view-restore", Gtk.IconSize.LARGE_TOOLBAR)
            )
            self._fs_button.set_tooltip_text("Quitter le plein écran (F11)")

    def _on_key_press(self, widget, event) -> bool:
        kv = event.keyval
        if kv == Gdk.KEY_Escape:
            self.close()
        elif kv in (Gdk.KEY_f, Gdk.KEY_F11):
            self._toggle_fullscreen()
        elif kv == Gdk.KEY_Right:
            self._seek_relative(+10)
        elif kv == Gdk.KEY_Left:
            self._seek_relative(-10)
        elif kv == Gdk.KEY_Up:
            self._seek_relative(+30)
        elif kv == Gdk.KEY_Down:
            self._seek_relative(-30)
        return True

    def _on_destroy(self, widget) -> None:
        if self._progress_timer:
            GLib.source_remove(self._progress_timer)
            self._progress_timer = None
        if self._seek_hide_timer:
            GLib.source_remove(self._seek_hide_timer)
            self._seek_hide_timer = None
        self._pipeline.set_state(self._Gst.State.NULL)
        if self._on_close_cb:
            cb = self._on_close_cb
            self._on_close_cb = None
            cb()

    def close(self) -> None:
        self._window.destroy()


# Sous-processus PIP lancé avec GDK_BACKEND=x11 (XWayland).
# set_keep_above(True) est ignoré sous Wayland pur (xdg-shell n'expose pas
# "always-on-top" pour les fenêtres normales). Avec x11/XWayland, GTK pose
# _NET_WM_STATE_ABOVE que GNOME Shell respecte.
_PIP_SCRIPT = """\
import sys, gi
gi.require_version('Gtk', '3.0')
gi.require_version('WebKit2', '4.1')
from gi.repository import Gtk, WebKit2
win = Gtk.Window()
win.set_title('WalkingPad PIP')
win.set_default_size(480, 270)
win.set_keep_above(True)
win.set_resizable(True)
wv = WebKit2.WebView()
wv.load_uri(sys.argv[1])
win.add(wv)
win.show_all()
win.connect('destroy', Gtk.main_quit)
Gtk.main()
"""


class HikingSimWindow:
    """
    Fenêtre de simulation 3D via Chromium en mode --app.
    Remplace WebKit2GTK pour éviter le tearing diagonal (back-slash) causé
    par le pipeline DMA-buf asynchrone de WebKit2GTK/Wayland.

    Un mini serveur HTTP sert l'HTML et expose /api/treadmill (JSON).
    Chromium est lancé en subprocess, la fenêtre est gérée par Chromium.
    """

    HTML_PATH = Path(__file__).parent / "forest.html"

    def __init__(self, on_close_cb, html_filename: str = "forest.html") -> None:
        self._on_close_cb  = on_close_cb
        self._html_filename = html_filename
        self._proc        = None
        self._http_server = None
        self._api_data    = {'speed': 0.0, 'dist': 0, 'steps': 0, 'elapsed': 0}
        self._api_lock    = threading.Lock()
        self._pip_proc: Optional[subprocess.Popen] = None

        import random
        self._seed = random.randint(-100, 100)
        port = self._start_http_server()
        url  = f'http://localhost:{port}/{html_filename}?seed={self._seed}'
        self._proc = subprocess.Popen(
            [
                'chromium',
                f'--app={url}',
                '--window-size=1280,720',
                '--no-default-browser-check',
                '--no-first-run',
                '--disable-infobars',
                '--ozone-platform=wayland',  # backend Wayland natif → tear-free (sans XWayland)
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Surveiller la fermeture de Chromium pour déclencher le callback
        GLib.timeout_add(1000, self._check_process)

    def _start_http_server(self) -> int:
        """Démarre un serveur HTTP dans un thread daemon, retourne le port."""
        with socket.socket() as s:
            s.bind(('localhost', 0))
            port = s.getsockname()[1]

        html_dir    = str(self.HTML_PATH.parent)
        api_data    = self._api_data
        api_lock    = self._api_lock
        pip_trigger = self._launch_pip   # référence pour la closure

        class _Handler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=html_dir, **kwargs)

            def do_GET(self):
                if self.path.startswith('/api/treadmill'):
                    with api_lock:
                        body = json.dumps(api_data).encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Cache-Control', 'no-cache')
                    self.end_headers()
                    self.wfile.write(body)
                elif self.path.startswith('/api/pip'):
                    from urllib.parse import urlparse, parse_qs
                    qs   = parse_qs(urlparse(self.path).query)
                    seed = qs.get('seed', ['0'])[0]
                    GLib.idle_add(pip_trigger, seed)
                    self.send_response(204)
                    self.end_headers()
                else:
                    super().do_GET()

            def log_message(self, *args):
                pass  # pas de logs HTTP dans la console

        self._http_server = http.server.HTTPServer(('localhost', port), _Handler)
        t = threading.Thread(target=self._http_server.serve_forever, daemon=True)
        t.start()
        return port

    def _launch_pip(self, seed: str = '0') -> bool:
        """
        Lance la fenêtre PIP dans un sous-processus avec GDK_BACKEND=x11.
        XWayland honore _NET_WM_STATE_ABOVE (set_keep_above), contrairement
        au backend Wayland natif qui ignore cette requête.
        Appelé depuis GLib.idle_add (thread principal GTK).
        """
        if self._pip_proc and self._pip_proc.poll() is None:
            return False   # déjà ouverte
        try:
            port = self._http_server.server_address[1]
            url  = f'http://localhost:{port}/{self._html_filename}?seed={seed}&pip=1'
            env  = os.environ.copy()
            env['GDK_BACKEND'] = 'x11'
            self._pip_proc = subprocess.Popen(
                [sys.executable, '-c', _PIP_SCRIPT, url],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as exc:
            logging.warning("Impossible de lancer le PIP : %s", exc)
        return False  # GLib.idle_add ne répète pas

    def update_treadmill_info(self, data: dict) -> None:
        """Met à jour les données tapis exposées via /api/treadmill."""
        with self._api_lock:
            self._api_data['speed']   = data.get('speed', 0.0)
            self._api_data['dist']    = data.get('distance', 0)
            self._api_data['steps']   = data.get('steps', 0)
            self._api_data['elapsed'] = data.get('time', 0)

    def _check_process(self) -> bool:
        """GLib timer : vérifie si Chromium tourne encore."""
        if self._proc and self._proc.poll() is not None:
            self._cleanup()
            return False   # arrête le timer
        return True        # continue

    def _cleanup(self) -> None:
        if self._http_server:
            self._http_server.shutdown()
            self._http_server = None
        if self._on_close_cb:
            cb, self._on_close_cb = self._on_close_cb, None
            GLib.idle_add(cb)

    def close(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
        self._cleanup()


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

        # --- fenêtres de statistiques (thread GTK uniquement) ---
        self._stats_window: Optional[Gtk.Window] = None
        self._detail_window: Optional[Gtk.Window] = None
        self._hiking_window:      Optional[HikingVideoWindow] = None
        self._sim_window:         Optional[HikingSimWindow]   = None
        self._sim_window_lane:    Optional[HikingSimWindow]   = None

    # ------------------------------------------------------------------
    # Thread principal — GTK
    # ------------------------------------------------------------------

    @staticmethod
    def _make_icon_item(label: str, icon_name: str) -> Gtk.ImageMenuItem:
        """Crée un ImageMenuItem avec icône (déprécié GTK mais requis par libdbusmenu/AppIndicator)."""
        item = Gtk.ImageMenuItem(label=label)
        img  = Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.MENU)
        item.set_image(img)
        item.set_always_show_image(True)
        return item

    def _build_indicator(self) -> None:
        self.indicator = AyatanaAppIndicator3.Indicator.new(
            "walkingpad-indicator",
            "media-playback-start",
            AyatanaAppIndicator3.IndicatorCategory.APPLICATION_STATUS,
        )
        self.indicator.set_status(AyatanaAppIndicator3.IndicatorStatus.ACTIVE)
        self.indicator.set_label(self.LABEL_DISCONNECTED, self.LABEL_GUIDE)

        self.menu = Gtk.Menu()

        item_stats = self._make_icon_item("Statistiques", "utilities-system-monitor")
        item_stats.connect("activate", self._on_show_stats)
        self.menu.append(item_stats)

        item_hiking = self._make_icon_item("Randonnée", "folder-videos")
        item_hiking.connect("activate", self._on_show_hiking_videos)
        self.menu.append(item_hiking)

        item_3d = self._make_icon_item("Parcours 3D", "applications-graphics")
        submenu_3d = Gtk.Menu()
        item_3d.set_submenu(submenu_3d)

        item_sim = self._make_icon_item("Forêt", "weather-overcast")
        item_sim.connect("activate", self._on_show_sim)
        submenu_3d.append(item_sim)

        item_sim_lane = self._make_icon_item("Chemin de campagne", "image-x-generic")
        item_sim_lane.connect("activate", self._on_show_sim_lane)
        submenu_3d.append(item_sim_lane)

        submenu_3d.show_all()
        self.menu.append(item_3d)

        self._item_pause = Gtk.CheckMenuItem(label="Veille (pause connexion)")
        self._item_pause.connect("toggled", self._on_toggle_pause)
        self.menu.append(self._item_pause)

        self.menu.append(Gtk.SeparatorMenuItem())

        item_restart = self._make_icon_item("Redémarrer", "view-refresh")
        item_restart.connect("activate", self._on_restart)
        self.menu.append(item_restart)

        self.menu.append(Gtk.SeparatorMenuItem())

        item_quit = self._make_icon_item("Quitter", "application-exit")
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
        if self._hiking_window:
            self._hiking_window.update_treadmill_info(self._treadmill_data)
        if self._sim_window:
            self._sim_window.update_treadmill_info(self._treadmill_data)
        if self._sim_window_lane:
            self._sim_window_lane.update_treadmill_info(self._treadmill_data)
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
        win.set_default_size(860, 900)
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

        # Temps équivalent à 2,5 km/h (en minutes)
        daily_equiv = {d: daily_dist[d] / 2.5 * 60 for d in all_days}

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

        # Valeurs cumulées (30 jours)
        cum_dist  = []
        cum_stp   = []
        cum_dur   = []
        cum_equiv = []
        _cd, _cs, _cu, _ce = 0.0, 0, 0.0, 0.0
        for d in all_days:
            _cd += daily_dist[d];  cum_dist.append(_cd)
            _cs += daily_stp[d];   cum_stp.append(_cs)
            _cu += daily_dur[d];   cum_dur.append(_cu)
            _ce += daily_equiv[d]; cum_equiv.append(_ce)

        cum_color = '#f0c040'   # jaune/or pour toutes les lignes cumulées

        def _add_cumline(ax, yvals, ylabel):
            """Superpose la ligne cumulée sur un axe droit.
            Le twin est mis en arrière-plan (zorder bas) pour ne pas bloquer
            les événements pick/mouse de l'axe principal."""
            axr = ax.twinx()
            axr.plot(x, yvals, color=cum_color, linewidth=1.5,
                     marker='.', markersize=4)
            axr.set_ylabel(ylabel, color=cum_color, fontsize=8)
            axr.tick_params(axis='y', colors=cum_color, labelsize=7)
            axr.spines['right'].set_color(cum_color)
            # Laisser passer les événements vers l'axe principal
            axr.set_zorder(ax.get_zorder() - 1)
            ax.patch.set_visible(False)   # fond transparent pour voir la ligne
            return axr

        # Figure matplotlib
        fig = Figure(figsize=(9, 9), tight_layout=True)
        color = '#4c9be8'

        ax1 = fig.add_subplot(4, 1, 1)
        bars1 = ax1.bar(x, [daily_dist[d] for d in all_days], color=color)
        ax1.set_ylabel('km / jour')
        ax1.set_title('Distance par jour  +  cumulé (—)')
        ax1.set_xticks(x)
        ax1.set_xticklabels([])
        _add_cumline(ax1, cum_dist, 'km cumulé')

        ax2 = fig.add_subplot(4, 1, 2, sharex=ax1)
        bars2 = ax2.bar(x, [daily_stp[d] for d in all_days], color='#6cc86c', picker=True)
        ax2.set_ylabel('pas / jour')
        ax2.set_title('Pas par jour  +  cumulé (—)')
        ax2.set_xticklabels([])
        _add_cumline(ax2, [v / 1000 for v in cum_stp], 'K pas cumulés')

        ax3 = fig.add_subplot(4, 1, 3, sharex=ax1)
        bars3 = ax3.bar(x, [daily_dur[d] for d in all_days], color='#e88c4c')
        ax3.set_ylabel('min / jour')
        ax3.set_title('Durée par jour  +  cumulé (—)')
        ax3.set_xticks(x)
        ax3.set_xticklabels([])
        _add_cumline(ax3, [v / 60 for v in cum_dur], 'h cumulées')

        ax4 = fig.add_subplot(4, 1, 4, sharex=ax1)
        bars4 = ax4.bar(x, [daily_equiv[d] for d in all_days], color='#c87cc8')
        ax4.axhline(y=90, color='#888888', linestyle='--', linewidth=1, label='1h30')
        ax4.set_ylabel('min éq. 2,5 km/h')
        ax4.set_title('Temps équivalent à 2,5 km/h  +  cumulé (—)')
        ax4.set_xticks(x)
        ax4.set_xticklabels(labels, rotation=45, ha='right', fontsize=8)
        _add_cumline(ax4, [v / 60 for v in cum_equiv], 'h cumulées')

        canvas = FigureCanvas(fig)
        canvas.set_size_request(840, 780)
        vbox.pack_start(canvas, True, True, 0)

        # Clic sur une barre de pas → détail horaire
        def _on_pick(event):
            if event.artist.axes is not ax2:
                return
            for bar in bars2:
                bar.set_facecolor('#6cc86c')
            event.artist.set_facecolor('#3a7a3a')
            fig.canvas.draw_idle()
            idx = list(bars2).index(event.artist)
            self._open_hourly_detail(all_days[idx], sessions)

        fig.canvas.mpl_connect('pick_event', _on_pick)

        # Tooltip au survol des barres + curseur pointer sur ax2
        _tip_kw = dict(xy=(0, 0), xytext=(0, 10), textcoords='offset points',
                       bbox=dict(boxstyle='round,pad=0.3',
                                 fc='#ffffdd', ec='#888888', alpha=0.9),
                       fontsize=8, ha='center', va='bottom', annotation_clip=False)
        _tooltips = {}
        for ax in (ax1, ax2, ax3, ax4):
            tip = ax.annotate('', **_tip_kw)
            tip.set_visible(False)
            _tooltips[ax] = tip

        _bar_map = {
            ax1: (bars1, [daily_dist[d] for d in all_days],  '{:.2f} km'),
            ax2: (bars2, [daily_stp[d] for d in all_days],   '{:,} pas'),
            ax3: (bars3, [daily_dur[d] for d in all_days],   '{:.0f} min'),
            ax4: (bars4, [daily_equiv[d] for d in all_days], '{:.0f} min'),
        }

        def _on_motion(event):
            gdk_win = canvas.get_window()
            if gdk_win is None:
                return
            # Curseur pointer sur ax2 (barres cliquables)
            if event.inaxes is ax2:
                gdk_win.set_cursor(Gdk.Cursor.new_from_name(canvas.get_display(), 'pointer'))
            else:
                gdk_win.set_cursor(None)
            # Tooltip
            found = False
            if event.inaxes in _bar_map:
                bars, vals, fmt = _bar_map[event.inaxes]
                tip = _tooltips[event.inaxes]
                for i, bar in enumerate(bars):
                    if bar.contains(event)[0]:
                        tip.set_text(fmt.format(vals[i]))
                        tip.xy = (bar.get_x() + bar.get_width() / 2,
                                  bar.get_height())
                        tip.set_visible(True)
                        found = True
                        break
                if not found:
                    tip.set_visible(False)
            # Masquer les tooltips des autres axes
            for ax, tip in _tooltips.items():
                if ax is not event.inaxes:
                    tip.set_visible(False)
            fig.canvas.draw_idle()

        fig.canvas.mpl_connect('motion_notify_event', _on_motion)

        win.show_all()

    def _compute_hourly_steps(self, day_str: str, sessions: list) -> dict:
        """Retourne un dict {heure: pas} pour le jour day_str.

        Les pas d'une session sont distribués proportionnellement au temps
        passé dans chaque tranche horaire.
        """
        hourly: dict = {h: 0 for h in range(24)}
        for s in sessions:
            if s.get('date', '') != day_str:
                continue
            steps = s.get('steps', 0)
            if steps == 0:
                continue
            total_s = s.get('duration_s', 0)
            try:
                start = datetime.datetime.fromisoformat(s['start'])
                end   = datetime.datetime.fromisoformat(s['end'])
            except (KeyError, ValueError):
                continue
            if total_s <= 0:
                hourly[start.hour] += steps
                continue
            current = start
            while current < end:
                next_hour = (current.replace(minute=0, second=0, microsecond=0)
                             + datetime.timedelta(hours=1))
                slice_end = min(next_hour, end)
                slice_s   = (slice_end - current).total_seconds()
                hourly[current.hour] += round(steps * slice_s / total_s)
                current = slice_end
        return hourly

    def _open_hourly_detail(self, day_str: str, sessions: list) -> None:
        """Ouvre une fenêtre affichant les pas par heure pour un jour donné."""
        if self._detail_window and self._detail_window.get_realized():
            self._detail_window.destroy()

        import matplotlib
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_gtk3agg import FigureCanvasGTK3Agg as FigureCanvas

        hourly    = self._compute_hourly_steps(day_str, sessions)
        hours     = list(range(24))
        step_vals = [hourly[h] for h in hours]
        labels    = [f"{h:02d}h" for h in hours]

        win = Gtk.Window(title=f"WalkingPad — Pas par heure · {day_str}")
        win.set_default_size(640, 400)
        win.connect("destroy", lambda w: setattr(self, '_detail_window', None))
        self._detail_window = win

        fig = Figure(figsize=(7, 4), tight_layout=True)
        ax  = fig.add_subplot(1, 1, 1)
        ax.bar(hours, step_vals, color='#6cc86c')
        for h, v in zip(hours, step_vals):
            if v > 0:
                ax.text(h, v, str(v), ha='center', va='bottom', fontsize=8)
        ax.set_xticks(hours)
        ax.set_xticklabels(labels, rotation=45, ha='right', fontsize=8)
        ax.set_ylabel('Pas')
        ax.set_title(f'Pas par heure — {day_str}')
        active = [h for h in hours if step_vals[h] > 0]
        if active:
            ax.set_xlim(active[0] - 0.7, active[-1] + 0.7)

        canvas = FigureCanvas(fig)
        canvas.set_size_request(620, 340)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        vbox.set_margin_top(8)
        vbox.set_margin_bottom(8)
        vbox.set_margin_start(8)
        vbox.set_margin_end(8)
        vbox.pack_start(canvas, True, True, 0)
        win.add(vbox)
        win.show_all()

    def _on_show_hiking_videos(self, _source=None) -> None:
        """Ouvre un dialogue de sélection de vidéo, puis lance la lecture plein écran."""
        videos_dir = Path.home() / "Vidéos" / "hiking"
        if not videos_dir.is_dir():
            dlg = Gtk.MessageDialog(
                message_type=Gtk.MessageType.ERROR,
                buttons=Gtk.ButtonsType.OK,
                text=f"Dossier introuvable :\n{videos_dir}",
            )
            dlg.run()
            dlg.destroy()
            return

        exts = {'.mp4', '.mkv', '.avi', '.mov', '.webm'}
        files = sorted(f for f in videos_dir.iterdir() if f.is_file() and f.suffix.lower() in exts)

        if not files:
            dlg = Gtk.MessageDialog(
                message_type=Gtk.MessageType.INFO,
                buttons=Gtk.ButtonsType.OK,
                text=f"Aucune vidéo dans :\n{videos_dir}",
            )
            dlg.run()
            dlg.destroy()
            return

        # Un seul fichier → lancer directement
        if len(files) == 1:
            self._launch_hiking_video(str(files[0]))
            return

        # Dialogue de sélection
        selected = [None]

        dlg = Gtk.Dialog(title="Choisir une vidéo de randonnée")
        dlg.set_default_size(640, 420)
        dlg.add_button("Annuler", Gtk.ResponseType.CANCEL)

        liststore = Gtk.ListStore(str, str)
        for f in files:
            liststore.append([f.name, str(f)])

        treeview = Gtk.TreeView(model=liststore)
        col = Gtk.TreeViewColumn("Vidéo", Gtk.CellRendererText(), text=0)
        treeview.append_column(col)

        def on_row_activated(_tv, path, _col):
            selected[0] = liststore[path][1]
            dlg.response(Gtk.ResponseType.OK)

        treeview.connect("row-activated", on_row_activated)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.add(treeview)
        dlg.get_content_area().pack_start(scrolled, True, True, 0)
        dlg.show_all()

        response = dlg.run()
        video_path = selected[0]
        dlg.destroy()

        if response == Gtk.ResponseType.OK and video_path:
            self._launch_hiking_video(video_path)

    def _launch_hiking_video(self, video_path: str) -> None:
        """Ferme l'éventuelle fenêtre existante et lance la nouvelle vidéo."""
        if self._hiking_window:
            self._hiking_window.close()
        try:
            self._hiking_window = HikingVideoWindow(
                video_path,
                on_close_cb=lambda: setattr(self, '_hiking_window', None),
            )
        except Exception as exc:
            logging.error("Impossible de lancer la vidéo : %s", exc)
            self._hiking_window = None
            dlg = Gtk.MessageDialog(
                message_type=Gtk.MessageType.ERROR,
                buttons=Gtk.ButtonsType.OK,
                text=f"Erreur de lecture vidéo :\n{exc}",
            )
            dlg.run()
            dlg.destroy()

    def _on_show_sim(self, _source=None) -> None:
        """Ouvre ou ferme (toggle) la fenêtre Forêt 3D (Chromium + WebGL)."""
        if self._sim_window:
            self._sim_window.close()
            return

        if not HikingSimWindow.HTML_PATH.is_file():
            dlg = Gtk.MessageDialog(
                message_type=Gtk.MessageType.ERROR,
                buttons=Gtk.ButtonsType.OK,
                text=f"Fichier simulation introuvable :\n{HikingSimWindow.HTML_PATH}",
            )
            dlg.run()
            dlg.destroy()
            return

        try:
            self._sim_window = HikingSimWindow(
                on_close_cb=lambda: setattr(self, '_sim_window', None),
            )
        except Exception as exc:
            logging.error("Impossible d'ouvrir la forêt 3D : %s", exc)
            self._sim_window = None
            dlg = Gtk.MessageDialog(
                message_type=Gtk.MessageType.ERROR,
                buttons=Gtk.ButtonsType.OK,
                text=f"Erreur forêt 3D :\n{exc}",
            )
            dlg.run()
            dlg.destroy()

    def _on_show_sim_lane(self, _source=None) -> None:
        """Ouvre ou ferme (toggle) la fenêtre Chemin de campagne (Chromium + WebGL)."""
        if self._sim_window_lane:
            self._sim_window_lane.close()
            return

        lane_path = Path(__file__).parent / "english_lane_hike.html"
        if not lane_path.is_file():
            dlg = Gtk.MessageDialog(
                message_type=Gtk.MessageType.ERROR,
                buttons=Gtk.ButtonsType.OK,
                text=f"Fichier simulation introuvable :\n{lane_path}",
            )
            dlg.run()
            dlg.destroy()
            return

        try:
            self._sim_window_lane = HikingSimWindow(
                on_close_cb=lambda: setattr(self, '_sim_window_lane', None),
                html_filename="english_lane_hike.html",
            )
        except Exception as exc:
            logging.error("Impossible d'ouvrir le chemin de campagne : %s", exc)
            self._sim_window_lane = None
            dlg = Gtk.MessageDialog(
                message_type=Gtk.MessageType.ERROR,
                buttons=Gtk.ButtonsType.OK,
                text=f"Erreur chemin de campagne :\n{exc}",
            )
            dlg.run()
            dlg.destroy()

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
