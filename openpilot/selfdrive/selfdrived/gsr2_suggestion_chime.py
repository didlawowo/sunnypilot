"""
Carillon des suggestions de dépassement — décide *quand* sonner, rien d'autre.

Le daemon ioniq-control publie sa suggestion dans /tmp/gsr2_state.json ; le
bandeau de la vue de conduite (patch 10) l'affiche. Ce module lit le même
fichier pour dire à `selfdrived` s'il faut poser l'événement qui produira le
son. Il ne joue rien lui-même : `soundd` est le seul à tenir le périphérique
audio.

Pourquoi un son du tout : sur l'essai routier du 2026-09-06, le daemon a émis
quatre suggestions de dépassement et le conducteur n'en a perçu aucune. Un
bandeau se rate quand on regarde la route ; c'est justement pour ça qu'un son
est plus intrusif, et qu'il ne doit sonner que lorsqu'il a quelque chose à
dire.

Trois règles, dans cet ordre d'importance :

1. **Une seule fois par suggestion.** L'identité d'une suggestion est son `ts`.
   Une même situation qui dure — le bandeau reste affiché tant qu'elle tient —
   ne produit qu'un seul carillon.
2. **Le dépassement seulement.** Le rabattement est moins urgent et le bandeau
   le porte déjà. Mesuré sur le trajet : 7 annonces en 37 min si on sonne pour
   tout, 5 si on ne sonne que sur le dépassement — une toutes les 7,4 minutes.
   Mettre `CHIME_ON` à ("overtake", "return") pour sonner sur les deux.
3. **Rien ne peut faire tomber `selfdrived`.** Ce process est critique : une
   exception ici désengage la conduite, contrairement à l'UI où elle ne fait
   que masquer un bandeau. Toute lecture est donc enveloppée, et l'échec se
   traduit par « pas de carillon », jamais par une remontée d'exception.

Installé par le patch 11-overtake-chime du projet ioniq-control.
Ne pas éditer ici.
"""

from __future__ import annotations

import json
import time

STATE_FILE = "/tmp/gsr2_state.json"

# Types de suggestion qui méritent un son.
CHIME_ON = ("overtake",)

# Le daemon écrit le state file toutes les 2 s. Au-delà de trois écritures
# manquées, on ne sonne plus : une suggestion porte une échéance qui peut
# rester dans le futur plusieurs secondes après la mort du daemon, et sonner
# sur la foi d'un fichier mort serait pire que de se taire.
STATE_STALE_S = 6.0

# `selfdrived` tourne à 100 Hz. Lire le fichier à chaque tour serait cent accès
# disque par seconde dans un process critique, pour une information qui ne
# change qu'une fois toutes les deux secondes.
READ_INTERVAL_S = 0.5


class SuggestionChime:
    """Dit à `selfdrived` s'il faut poser l'événement du carillon."""

    def __init__(self, state_file: str = STATE_FILE) -> None:
        self._state_file = state_file
        self._read_at = 0.0
        self._state: dict | None = None
        # `ts` de la dernière suggestion annoncée. Sert d'identité : deux
        # suggestions distinctes ont des `ts` distincts, une suggestion qui
        # dure garde le sien.
        self._chimed_ts: float | None = None

    # ── Lecture ──────────────────────────────────────────────────

    def _refresh(self, now: float) -> None:
        if now - self._read_at < READ_INTERVAL_S:
            return
        self._read_at = now
        try:
            with open(self._state_file) as f:
                self._state = json.load(f)
        except (OSError, ValueError):
            self._state = None

    def _pending(self, now: float) -> dict | None:
        """Suggestion vivante qui mérite un son, ou None."""
        state = self._state
        if not isinstance(state, dict):
            return None
        if (now - float(state.get("_ts") or 0)) > STATE_STALE_S:
            return None
        suggestion = state.get("suggestion")
        if not isinstance(suggestion, dict):
            return None
        if suggestion.get("kind") not in CHIME_ON:
            return None
        try:
            if now >= float(suggestion["expires_at"]):
                return None
            float(suggestion["ts"])
        except (KeyError, TypeError, ValueError):
            return None
        return suggestion

    # ── Décision ─────────────────────────────────────────────────

    def should_chime(self, now: float | None = None) -> bool:
        """True une seule fois par suggestion, jamais deux.

        Ne lève jamais : `selfdrived` désengage la conduite s'il tombe.
        """
        try:
            now = time.time() if now is None else now
            self._refresh(now)
            suggestion = self._pending(now)
            if suggestion is None:
                return False
            ts = float(suggestion["ts"])
            if ts == self._chimed_ts:
                return False
            self._chimed_ts = ts
            return True
        except Exception:  # noqa: BLE001 — rien ne doit faire tomber selfdrived
            return False
