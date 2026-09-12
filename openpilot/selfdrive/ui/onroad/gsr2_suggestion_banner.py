"""
Bandeau de suggestion de dépassement sur la vue de conduite.

Le daemon ioniq-control décide (`src/daemon/suggestion.py`) et publie dans
/tmp/gsr2_state.json ; ce bandeau ne fait que montrer. Il ne commande rien :
c'est le conducteur qui met son clignotant, et `AutoLaneChangeTimer` de
sunnypilot qui exécute le changement de voie.

Il reste affiché tant que la situation tient — c'est le daemon qui repousse
`expires_at` à chaque pas de boucle — et la barre du bas mesure la fenêtre de
grâce, donc annonce qu'il est sur le point de s'effacer. La première version
s'affichait 8 s puis disparaissait : le 2026-09-06, derrière un camping-car,
elle a montré son bandeau de 12:11:39 à 12:11:47 pour une situation qui a duré
jusqu'à 12:12:46, et le conducteur n'a rien vu.

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

# Dimensions revues après le premier essai routier (2026-09-06) : 620×110 avec
# un texte de 44 sur un écran de 2160×1080, ça ne s'attrape pas du coin de
# l'œil en roulant. Le bandeau occupe désormais 42 % de la largeur.
BANNER_W = 900
BANNER_H = 170
# Au-dessus du bouton de capture (patch 08), qui occupe la même colonne : son
# bord haut est à 150 px du bas (BOTTOM_MARGIN 60 + BTN_H 90).
BANNER_BOTTOM_MARGIN = 190
EDGE_W = 18  # bande latérale : porte la direction, sans glyphe
FONT_SIZE = 64
BAR_H = 8

COLOR_BG = rl.Color(15, 23, 42, 225)
COLOR_BORDER = rl.Color(148, 163, 184, 130)
COLOR_TEXT = rl.Color(248, 250, 252, 255)
COLOR_OVERTAKE = rl.Color(56, 189, 248, 255)
COLOR_RETURN = rl.Color(74, 222, 128, 255)
COLOR_BAR_BG = rl.Color(51, 65, 85, 200)

COLORS = {"overtake": COLOR_OVERTAKE, "return": COLOR_RETURN}

# Retour du bouton ☆ (#187) : le daemon publie `star_feedback` {text, ts,
# expires_at}. Même emplacement que la suggestion, moins haut, pas de barre :
# c'est un accusé de réception, pas une invitation à agir.
FEEDBACK_H = 110
FEEDBACK_FONT_SIZE = 52
COLOR_FEEDBACK = rl.Color(250, 204, 21, 255)


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

    def _stale(self, now: float) -> bool:
        """Vrai si le state file manque, n'est pas lisible, ou date trop.

        `_ts` est converti ici et nulle part ailleurs : une valeur non
        numérique lèverait dans la boucle de rendu, ce que le contrat de ce
        fichier interdit — on la traite comme un fichier périmé.
        """
        state = self._state
        if not isinstance(state, dict):
            return True
        try:
            age = now - float(state.get("_ts") or 0)
        except (TypeError, ValueError):
            return True
        return age > STATE_STALE_S

    def current(self, now: float) -> dict | None:
        """Suggestion à afficher, ou None.

        Trois conditions, chacune nécessaire : un state file frais (le daemon
        vit), une suggestion présente, et une échéance encore à venir.
        """
        state = self._state
        if self._stale(now) or not isinstance(state, dict):
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

    @staticmethod
    def _remaining(suggestion: dict, now: float) -> float:
        """Fraction de la grâce qu'il reste avant que le bandeau s'efface.

        Le daemon repousse `expires_at` tant que la situation tient : la barre
        ne mesure donc pas la durée totale d'affichage (elle n'est pas connue
        d'avance) mais la fenêtre de grâce, qui ne se vide que lorsque
        l'opportunité a disparu. `grace_s` peut manquer si le daemon est plus
        ancien que ce bandeau — on retombe alors sur l'écart d'origine.
        """
        expires_at = float(suggestion["expires_at"])
        try:
            span = float(suggestion.get("grace_s") or 0.0)
        except (TypeError, ValueError):
            span = 0.0
        if span <= 0:
            span = expires_at - float(suggestion["ts"])
        if span <= 0:
            return 0.0
        return max(0.0, min(1.0, (expires_at - now) / span))

    def current_feedback(self, now: float) -> dict | None:
        """Retour ☆ à afficher, ou None : state file frais et échéance à venir."""
        state = self._state
        if self._stale(now) or not isinstance(state, dict):
            return None
        fb = state.get("star_feedback")
        if not isinstance(fb, dict) or not isinstance(fb.get("text"), str) or not fb["text"]:
            return None
        try:
            if now >= float(fb["expires_at"]):
                return None
        except (KeyError, TypeError, ValueError):
            return None
        return fb

    # ── Rendu ────────────────────────────────────────────────────

    def render(self, rect: rl.Rectangle) -> None:
        now = time.time()
        self._refresh(now)
        suggestion = self.current(now)
        if suggestion is None:
            feedback = self.current_feedback(now)
            if feedback is not None:
                self._render_feedback(rect, feedback["text"])
            return

        kind = suggestion["kind"]
        accent = COLORS[kind]
        remaining = self._remaining(suggestion, now)

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
            rl.Rectangle(bar_x, bar_y, bar_w * remaining, BAR_H),
            0.5,
            4,
            accent,
        )

    def _render_feedback(self, rect: rl.Rectangle, text: str) -> None:
        """Accusé de réception du bouton ☆ : bandeau bref, sans barre ni direction."""
        banner = rl.Rectangle(
            rect.x + (rect.width - BANNER_W) / 2,
            rect.y + rect.height - FEEDBACK_H - BANNER_BOTTOM_MARGIN,
            BANNER_W,
            FEEDBACK_H,
        )
        rl.draw_rectangle_rounded(banner, 0.25, 10, COLOR_BG)
        rl.draw_rectangle_rounded_lines_ex(banner, 0.25, 10, 2, COLOR_FEEDBACK)
        size = measure_text_cached(self._font, text, FEEDBACK_FONT_SIZE)
        rl.draw_text_ex(
            self._font,
            text,
            rl.Vector2(banner.x + (BANNER_W - size.x) / 2, banner.y + (FEEDBACK_H - size.y) / 2),
            FEEDBACK_FONT_SIZE,
            0,
            COLOR_TEXT,
        )
