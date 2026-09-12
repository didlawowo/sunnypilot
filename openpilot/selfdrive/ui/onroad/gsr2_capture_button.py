"""
Bouton de capture CAN dans la vue de conduite.

Une capture s'utilise au moment où quelque chose se produit — un bip
inattendu, un affichage inhabituel, un comportement ADAS à documenter. Le
bouton équivalent existe dans le panel des Settings, mais le temps d'y
accéder l'événement est passé, et c'est précisément l'échantillon qu'on
voulait. D'où ce déclencheur directement sur la vue route.

L'appui est un interrupteur : le premier démarre la capture, le suivant
l'arrête. La première version ne savait que démarrer — il fallait rejoindre
le menu pour couper, et un appui manqué de quelques pixels ouvrait le menu
latéral, ce qui donnait l'impression d'un bouton qu'on ne peut pas éteindre
(#111).

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

# Le basculement en « hors ligne » exige DEUX lectures d'affilée qui le
# constatent. Une seule suffisait, et le bouton clignotait en gris au retour
# d'une capture alors que le daemon n'avait rien eu : ni blocage de boucle, ni
# erreur d'écriture dans ses journaux du 2026-09-07. Ce n'est pas le daemon qui
# était absent, c'est la boucle de rendu qui n'avait pas relu à temps — l'UI
# d'openpilot est bousculée par camerad et modeld — et le bouton prenait sa
# propre lecture vieillie pour un daemon mort.
#
# Deux lectures espacées d'au moins une seconde : un à-coup de rendu ne
# déclenche plus rien, une vraie chute reste signalée en deux secondes.
OFFLINE_CONFIRMATIONS = 2
# Plafond de durée demandé au daemon. Ce n'est plus la durée nominale depuis
# que l'appui est un interrupteur : c'est le conducteur qui coupe, et ce
# plafond n'est qu'un filet si personne ne le fait. Il aligne la capture
# manuelle sur la capture automatique (auto_capture_duration_s = 120).
CAPTURE_MAX_DURATION_S = 120
# Fenêtre pendant laquelle on affiche l'accusé de réception, le temps que le
# daemon publie l'état "capture en cours" dans le state file.
ACK_WINDOW_S = 3.0

BTN_W = 210
BTN_H = 90
# Bas au centre : le coin bas gauche portait les informations de développement
# de sunnypilot, que le bouton masquait (trajet du 2026-09-06). Le centre est
# aussi la colonne du bandeau de suggestion (patch 10), qui se pose au-dessus.
BOTTOM_MARGIN = 60
# Marge de tolérance autour du bouton pour l'appui. Un appui manqué de quelques
# pixels retombe sur la vue route, qui ouvre le menu latéral : c'est ce qu'on a
# constaté en voulant couper une capture en roulant (#111). Viser un rectangle
# de 210×90 sur une route qui bouge mérite cette marge.
HIT_PAD = 24
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
        # Lectures consécutives ayant conclu à un daemon absent, et raison de
        # la dernière. Deux causes très différentes affichaient le même gris :
        # « je n'ai pas pu lire le fichier » et « le fichier est vieux ».
        self._offline_streak = 0
        self._offline_reason = ""
        # Commande envoyée dont on attend encore l'effet dans le state file :
        # "capture_start" ou "capture_stop". Le daemon publie son état toutes
        # les 2 s, l'appui doit donc dire quelque chose avant ça.
        #
        # Ce que ce quelque chose dit compte : « envoyé » décrivait la
        # plomberie — une commande écrite dans un fichier — et ne répondait
        # pas à la question du conducteur, qui est « est-ce que ça enregistre
        # ? ». Les libellés annoncent donc l'effet attendu, pas le mécanisme.
        self._pending: str | None = None

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
            fresh = (now - float(self._state.get("_ts", 0))) < STATE_STALE_S
            raison = "" if fresh else "état périmé"
        except (OSError, ValueError) as e:
            self._state = None
            fresh = False
            raison = "fichier illisible" if isinstance(e, OSError) else "fichier corrompu"

        if fresh:
            self._offline_streak = 0
            self._offline_reason = ""
        else:
            self._offline_streak += 1
            self._offline_reason = raison

    def _daemon_online(self) -> bool:
        """Le daemon est-il vivant, autant qu'on puisse en juger d'ici ?

        On ne le déclare absent qu'après plusieurs constats d'affilée : une
        lecture manquée n'est pas un daemon mort, et l'annoncer sur un seul
        échantillon faisait clignoter le bouton pendant que tout allait bien.
        """
        return self._offline_streak < OFFLINE_CONFIRMATIONS

    def _capture_active(self) -> bool:
        return bool(self._state and self._state.get("capture_active"))

    def _label(self) -> tuple[str, rl.Color, rl.Color]:
        """Libellé, fond et couleur de texte selon l'état."""
        if not self._daemon_online():
            # La cause est affichée : « périmé » veut dire que le daemon
            # n'écrit plus, « illisible » que le fichier a disparu. Deux pannes
            # différentes qui montraient jusqu'ici le même gris, et qu'on ne
            # pouvait donc pas départager depuis la route.
            return f"hors ligne - {self._offline_reason}", COLOR_OFFLINE, COLOR_TEXT_OFFLINE
        active = self._capture_active()
        pending = self._pending if (time.time() - self._sent_at) < ACK_WINDOW_S else None
        if pending is None:
            # Le state file a eu le temps de refléter la commande, ou elle a
            # été perdue : dans les deux cas c'est lui qui fait foi.
            self._pending = None
        if pending == "capture_stop" and active:
            return "ARRÊT...", COLOR_ACK, COLOR_TEXT
        if active:
            return "● STOP", COLOR_ACTIVE, COLOR_TEXT
        if pending == "capture_start":
            return "DÉMARRAGE...", COLOR_ACK, COLOR_TEXT
        return "CAPTURE", COLOR_IDLE, COLOR_TEXT

    # ── Rendu ────────────────────────────────────────────────────

    def render(self, rect: rl.Rectangle) -> None:
        self._refresh_state()

        # Bas au centre : laisse libres le coin bas gauche (informations de
        # développement), le haut (vitesse, panneaux) et la droite (alertes).
        self._rect = rl.Rectangle(
            rect.x + (rect.width - BTN_W) / 2,
            rect.y + rect.height - BTN_H - BOTTOM_MARGIN,
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

    def hit_rect(self) -> rl.Rectangle:
        """Zone sensible : le bouton, élargi de `HIT_PAD` sur les quatre côtés."""
        return rl.Rectangle(
            self._rect.x - HIT_PAD,
            self._rect.y - HIT_PAD,
            self._rect.width + 2 * HIT_PAD,
            self._rect.height + 2 * HIT_PAD,
        )

    def handle_click(self, pos) -> bool:
        """Traite un appui. Retourne True si le bouton l'a consommé.

        Le retour conditionne la suite : la vue route déclenche sa propre
        action sur un tap — ouvrir le menu latéral —, il ne faut pas qu'un
        appui sur le bouton la déclenche aussi.

        L'appui est un interrupteur : il démarre la capture, et le suivant
        l'arrête. Sans ça il n'y avait aucun moyen de couper depuis la route,
        il fallait rejoindre le menu (#111).
        """
        if self._rect.width <= 0 or not rl.check_collision_point_rec(pos, self.hit_rect()):
            return False
        # Hors ligne : l'appui est consommé (c'est un bouton, pas la route en
        # dessous) mais rien n'est envoyé — une commande sans daemon pour la
        # lire resterait dans /tmp jusqu'au prochain démarrage.
        if self._daemon_online():
            if self._capture_active():
                self._send_command("capture_stop", {})
            else:
                self._send_command(
                    "capture_start",
                    {"max_duration": CAPTURE_MAX_DURATION_S, "deduplicate": True},
                )
        return True

    def _send_command(self, cmd_type: str, data: dict) -> None:
        """Écrit la commande IPC, au format que le daemon attend (core/ipc.py)."""
        payload = {"type": cmd_type, "data": data, "ts": time.time()}
        tmp = COMMAND_FILE + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(payload, f)
            os.replace(tmp, COMMAND_FILE)
            self._sent_at = time.time()
            self._pending = cmd_type
        except OSError:
            # Échec silencieux volontaire : rien ne doit pouvoir faire tomber
            # la vue de conduite. L'absence de retour visuel suffit à informer.
            pass
