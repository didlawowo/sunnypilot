"""
Bouton de capture CAN dans la vue de conduite.

Une capture s'utilise au moment où quelque chose se produit — un bip
inattendu, un affichage inhabituel, un comportement ADAS à documenter. Le
bouton équivalent existe dans le panel des Settings, mais le temps d'y
accéder l'événement est passé, et c'est précisément l'échantillon qu'on
voulait. D'où ce déclencheur directement sur la vue route.

Dialogue avec le daemon ioniq-control par les mêmes fichiers IPC que le
panel Settings : lecture de /tmp/gsr2_state.json, écriture de la commande
dans /tmp/gsr2_command.json.

Volontairement dessiné à la primitive, sans texture : un chemin d'icône
invalide produit une texture de largeur nulle, donc une ZeroDivisionError au
scaling, qui fait retomber l'UI sur l'accueil sans message (cf. gotchas UI
dans le CLAUDE.md du projet).

Installé par le patch 08-onroad-capture du projet ioniq-control.
Ne pas éditer ici.
"""

from __future__ import annotations

import json
import os
import time

import pyray as rl

from openpilot.system.ui.lib.application import FontWeight, gui_app
from openpilot.system.ui.lib.text_measure import measure_text_cached

STATE_FILE = "/tmp/gsr2_state.json"
COMMAND_FILE = "/tmp/gsr2_command.json"

# Au-delà, le daemon est considéré hors ligne : le bouton reste affiché, grisé
# et inactif. Le masquer était la première idée — mais un bouton absent ne dit
# pas s'il est masqué ou s'il n'est pas rendu du tout (trajet du 2026-08-30 :
# « le bouton n'apparaît pas », sans qu'on puisse trancher à distance). Un état
# visible « hors ligne » lève l'ambiguïté et signale en plus un daemon tombé.
STATE_STALE_S = 10.0
# Durée demandée au daemon pour une capture déclenchée depuis la route.
CAPTURE_DURATION_S = 10
# Fenêtre pendant laquelle on affiche l'accusé de réception, le temps que le
# daemon publie l'état "capture en cours" dans le state file.
ACK_WINDOW_S = 3.0

BTN_W = 210
BTN_H = 90
MARGIN = 30
FONT_SIZE = 38

COLOR_IDLE = rl.Color(30, 41, 59, 200)
COLOR_OFFLINE = rl.Color(71, 85, 105, 140)
COLOR_ACTIVE = rl.Color(220, 38, 38, 230)
COLOR_ACK = rl.Color(14, 165, 233, 230)
COLOR_TEXT = rl.Color(248, 250, 252, 255)
COLOR_TEXT_OFFLINE = rl.Color(203, 213, 225, 170)
COLOR_BORDER = rl.Color(148, 163, 184, 120)


class CaptureButtonRenderer:
    """Bouton flottant de déclenchement de capture, rendu sur la vue route."""

    def __init__(self) -> None:
        self._font = gui_app.font(FontWeight.SEMI_BOLD)
        self._rect = rl.Rectangle(0, 0, 0, 0)
        self._state: dict | None = None
        self._state_read_at = 0.0
        self._sent_at = 0.0

    # ── État du daemon ───────────────────────────────────────────

    def _refresh_state(self) -> None:
        """Relit le state file au plus une fois par seconde.

        Le daemon l'écrit toutes les 2 s ; le relire à chaque frame (20 Hz)
        n'apporterait rien et ferait des accès disque inutiles dans la boucle
        de rendu.
        """
        now = time.time()
        if now - self._state_read_at < 1.0:
            return
        self._state_read_at = now
        try:
            with open(STATE_FILE) as f:
                self._state = json.load(f)
        except (OSError, ValueError):
            self._state = None

    def _daemon_online(self) -> bool:
        if not self._state:
            return False
        ts = self._state.get("_ts", 0)
        return (time.time() - ts) < STATE_STALE_S

    def _capture_active(self) -> bool:
        return bool(self._state and self._state.get("capture_active"))

    def _label(self) -> tuple[str, rl.Color, rl.Color]:
        """Libellé, fond et couleur de texte selon l'état."""
        if not self._daemon_online():
            return "hors ligne", COLOR_OFFLINE, COLOR_TEXT_OFFLINE
        if self._capture_active():
            return "● CAPTURE", COLOR_ACTIVE, COLOR_TEXT
        if time.time() - self._sent_at < ACK_WINDOW_S:
            return "envoyé…", COLOR_ACK, COLOR_TEXT
        return "CAPTURE", COLOR_IDLE, COLOR_TEXT

    # ── Rendu ────────────────────────────────────────────────────

    def render(self, rect: rl.Rectangle) -> None:
        self._refresh_state()

        # Coin bas gauche : laisse libres le centre (trajectoire), le haut
        # (vitesse, panneaux) et la droite (alertes).
        self._rect = rl.Rectangle(
            rect.x + MARGIN,
            rect.y + rect.height - BTN_H - MARGIN,
            BTN_W,
            BTN_H,
        )

        label, color, text_color = self._label()
        rl.draw_rectangle_rounded(self._rect, 0.25, 10, color)
        rl.draw_rectangle_rounded_lines_ex(self._rect, 0.25, 10, 2, COLOR_BORDER)

        size = measure_text_cached(self._font, label, FONT_SIZE)
        rl.draw_text_ex(
            self._font,
            label,
            rl.Vector2(
                self._rect.x + (self._rect.width - size.x) / 2,
                self._rect.y + (self._rect.height - size.y) / 2,
            ),
            FONT_SIZE,
            0,
            text_color,
        )

    # ── Interaction ──────────────────────────────────────────────

    def handle_click(self, pos) -> bool:
        """Traite un appui. Retourne True si le bouton l'a consommé.

        Le retour conditionne la suite : la vue route déclenche sa propre
        action sur un tap, il ne faut pas qu'un appui sur le bouton la
        déclenche aussi.
        """
        if self._rect.width <= 0 or not rl.check_collision_point_rec(pos, self._rect):
            return False
        # Hors ligne : l'appui est consommé (c'est un bouton, pas la route en
        # dessous) mais rien n'est envoyé — une commande sans daemon pour la
        # lire resterait dans /tmp jusqu'au prochain démarrage.
        if self._daemon_online() and not self._capture_active():
            self._send_capture_command()
        return True

    def _send_capture_command(self) -> None:
        payload = {
            "type": "capture_start",
            "data": {"max_duration": CAPTURE_DURATION_S, "deduplicate": True},
            "ts": time.time(),
        }
        tmp = COMMAND_FILE + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(payload, f)
            os.replace(tmp, COMMAND_FILE)
            self._sent_at = time.time()
        except OSError:
            # Échec silencieux volontaire : rien ne doit pouvoir faire tomber
            # la vue de conduite. L'absence de retour visuel suffit à informer.
            pass
