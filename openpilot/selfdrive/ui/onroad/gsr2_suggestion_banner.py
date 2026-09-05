"""
Bandeau de suggestion de dépassement sur la vue de conduite.

Le daemon ioniq-control décide (`src/daemon/suggestion.py`) et publie dans
/tmp/gsr2_state.json ; ce bandeau ne fait que montrer. Il ne commande rien :
c'est le conducteur qui met son clignotant, et `AutoLaneChangeTimer` de
sunnypilot qui exécute le changement de voie.

Pas de carillon dans cette version. `soundd` ne joue que ce que
`selfdriveState.alertSound` lui demande, et ce topic appartient à `selfdrived` —
msgq n'autorise qu'un publisher. Le faire sonner supposerait soit de patcher le
chemin d'alerte (le patch ne serait plus cosmétique), soit de prendre le
périphérique audio sous soundd depuis l'UI. Les deux méritent leur propre
décision, pas d'être glissés dans un patch d'affichage.

Deux règles héritées des pannes passées :

- dessiné à la primitive, sans texture : un chemin d'icône invalide donne une
  texture de largeur nulle, donc une ZeroDivisionError au scaling, qui fait
  retomber l'UI sur l'accueil sans le moindre message ;
- rien de ce qui est lu ici ne peut faire tomber la vue de conduite. Un state
  file absent, tronqué ou périmé se traduit par un bandeau qui ne s'affiche
  pas — jamais par une exception.

Installé par le patch 10-onroad-suggestions du projet ioniq-control.
Ne pas éditer ici.
"""

from __future__ import annotations

import json
import time

import pyray as rl

from openpilot.system.ui.lib.application import FontWeight, gui_app
from openpilot.system.ui.lib.text_measure import measure_text_cached

STATE_FILE = "/tmp/gsr2_state.json"

# Le daemon écrit le state file toutes les 2 s. Au-delà de trois écritures
# manquées, on ne montre plus rien : une suggestion porte un `expires_at` qui
# peut rester dans le futur pendant plusieurs secondes après la mort du daemon,
# et afficher « double maintenant » sur la foi d'un fichier mort serait pire
# que de ne rien afficher.
STATE_STALE_S = 6.0

# Relecture du state file. La boucle de rendu tourne à 20 Hz ; lire le fichier
# à chaque frame n'apporterait rien qu'un accès disque de plus par frame.
READ_INTERVAL_S = 0.5

LABELS = {
    "overtake": "DOUBLER À GAUCHE",
    "return": "SE RABATTRE À DROITE",
}

BANNER_W = 620
BANNER_H = 110
BANNER_BOTTOM_MARGIN = 160  # au-dessus de la rangée du bouton de capture
EDGE_W = 14  # bande latérale : porte la direction, sans glyphe
FONT_SIZE = 44
BAR_H = 6

COLOR_BG = rl.Color(15, 23, 42, 225)
COLOR_BORDER = rl.Color(148, 163, 184, 130)
COLOR_TEXT = rl.Color(248, 250, 252, 255)
COLOR_OVERTAKE = rl.Color(56, 189, 248, 255)
COLOR_RETURN = rl.Color(74, 222, 128, 255)
COLOR_BAR_BG = rl.Color(51, 65, 85, 200)

COLORS = {"overtake": COLOR_OVERTAKE, "return": COLOR_RETURN}


class SuggestionBannerRenderer:
    """Bandeau éphémère, rendu seulement quand une suggestion est vivante."""

    def __init__(self) -> None:
        self._font = gui_app.font(FontWeight.BOLD)
        self._state: dict | None = None
        self._read_at = 0.0

    # ── Lecture de l'état ────────────────────────────────────────

    def _refresh(self, now: float) -> None:
        if now - self._read_at < READ_INTERVAL_S:
            return
        self._read_at = now
        try:
            with open(STATE_FILE) as f:
                self._state = json.load(f)
        except (OSError, ValueError):
            self._state = None

    def current(self, now: float) -> dict | None:
        """Suggestion à afficher, ou None.

        Trois conditions, chacune nécessaire : un state file frais (le daemon
        vit), une suggestion présente, et une échéance encore à venir.
        """
        state = self._state
        if not isinstance(state, dict):
            return None
        if (now - float(state.get("_ts") or 0)) > STATE_STALE_S:
            return None
        suggestion = state.get("suggestion")
        if not isinstance(suggestion, dict):
            return None
        if suggestion.get("kind") not in LABELS:
            return None
        try:
            expires_at = float(suggestion["expires_at"])
            ts = float(suggestion["ts"])
        except (KeyError, TypeError, ValueError):
            return None
        if now >= expires_at or expires_at <= ts:
            return None
        return suggestion

    # ── Rendu ────────────────────────────────────────────────────

    def render(self, rect: rl.Rectangle) -> None:
        now = time.time()
        self._refresh(now)
        suggestion = self.current(now)
        if suggestion is None:
            return

        kind = suggestion["kind"]
        accent = COLORS[kind]
        remaining = (float(suggestion["expires_at"]) - now) / (
            float(suggestion["expires_at"]) - float(suggestion["ts"])
        )

        banner = rl.Rectangle(
            rect.x + (rect.width - BANNER_W) / 2,
            rect.y + rect.height - BANNER_H - BANNER_BOTTOM_MARGIN,
            BANNER_W,
            BANNER_H,
        )
        rl.draw_rectangle_rounded(banner, 0.18, 10, COLOR_BG)
        rl.draw_rectangle_rounded_lines_ex(banner, 0.18, 10, 2, COLOR_BORDER)

        # Bande latérale du côté de la manœuvre : la direction se lit à la
        # position, plus vite qu'en lisant le texte, et sans dépendre d'un
        # glyphe de flèche que la police pourrait ne pas avoir.
        edge_x = banner.x if suggestion.get("direction") == "left" else banner.x + BANNER_W - EDGE_W
        rl.draw_rectangle_rounded(
            rl.Rectangle(edge_x, banner.y + 12, EDGE_W, BANNER_H - 24), 0.5, 6, accent
        )

        label = LABELS[kind]
        size = measure_text_cached(self._font, label, FONT_SIZE)
        rl.draw_text_ex(
            self._font,
            label,
            rl.Vector2(
                banner.x + (BANNER_W - size.x) / 2,
                banner.y + (BANNER_H - size.y) / 2 - BAR_H,
            ),
            FONT_SIZE,
            0,
            COLOR_TEXT,
        )

        # Temps restant : dit que la suggestion va disparaître, donc qu'elle
        # n'est pas un état permanent qu'on peut ignorer indéfiniment.
        bar_w = BANNER_W - 2 * (EDGE_W + 12)
        bar_x = banner.x + EDGE_W + 12
        bar_y = banner.y + BANNER_H - BAR_H - 14
        rl.draw_rectangle_rounded(rl.Rectangle(bar_x, bar_y, bar_w, BAR_H), 0.5, 4, COLOR_BAR_BG)
        rl.draw_rectangle_rounded(
            rl.Rectangle(bar_x, bar_y, max(0.0, bar_w * min(1.0, remaining)), BAR_H),
            0.5,
            4,
            accent,
        )
