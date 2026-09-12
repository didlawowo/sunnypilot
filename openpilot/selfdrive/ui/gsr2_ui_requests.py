"""
Demandes du daemon ioniq-control à l'UI sunnypilot (bouton ☆, issue #187).

Le daemon décide (`src/daemon/star_actions.py`) et publie dans
/tmp/gsr2_state.json un `ui_request` numéroté : `{"kind", "seq", "ts"}`. Ce
module le lit à chaque image du layout principal et exécute ce que le daemon
ne peut pas faire lui-même :

- `screen_off` : écran noir. La luminosité appartient au process UI, qui la
  recalcule à chaque image ; on ne la force pas de l'extérieur, on pose
  `ui_state.gsr2_screen_dark` et `DeviceSP.set_onroad_brightness` (patché)
  rend 0. Un toucher, la coupure du contact ou une nouvelle demande rallument.
- `panel_toggle` : ouvre le panneau IONIQ Control des Settings, ou revient à
  la route s'il est ouvert.

Chaque demande est exécutée UNE fois (`seq` mémorisé) et seulement si elle est
récente : au démarrage de l'UI, une demande vieille de dix minutes dans un
state file qui n'a pas bougé n'est pas un ordre, c'est une trace.

Deux règles héritées des pannes passées : rien de ce qui est lu ici ne peut
faire tomber la boucle de rendu (fichier absent, tronqué, périmé = aucune
action, jamais une exception), et aucune texture.

Installé par le patch 15-star-button-ui du projet ioniq-control.
Ne pas éditer ici.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable

STATE_FILE = "/tmp/gsr2_state.json"

# Le daemon écrit le state file toutes les 2 s (et dès qu'une demande apparaît).
READ_INTERVAL_S = 0.25
# Au-delà, une demande est une trace, pas un ordre.
REQUEST_MAX_AGE_S = 10.0

KIND_SCREEN_OFF = "screen_off"
KIND_PANEL_TOGGLE = "panel_toggle"


class UiRequestWatcher:
    """Lit `ui_request` dans le state file et ne rend chaque demande qu'une fois."""

    def __init__(self, state_file: str = STATE_FILE, max_age_s: float = REQUEST_MAX_AGE_S) -> None:
        self._state_file = state_file
        self._max_age_s = max_age_s
        self._read_at = 0.0
        self._last_seq: int | None = None

    def poll(self, now: float | None = None) -> dict | None:
        """Nouvelle demande fraîche, ou None. Ne lève jamais."""
        now = time.time() if now is None else now
        if now - self._read_at < READ_INTERVAL_S:
            return None
        self._read_at = now
        try:
            with open(self._state_file) as f:
                state = json.load(f)
            req = state.get("ui_request") if isinstance(state, dict) else None
            if not isinstance(req, dict):
                return None
            seq = int(req["seq"])
            ts = float(req["ts"])
            kind = str(req["kind"])
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if self._last_seq is None:
            # Première lecture : on prend acte de ce qui est là, sans jamais
            # l'exécuter. Une demande déjà honorée avant un redémarrage de l'UI
            # rebasculerait l'écran ou le panneau sans que personne n'ait
            # appuyé ; perdre un appui émis pendant le redémarrage est moins
            # grave — le conducteur appuie à nouveau.
            self._last_seq = seq
            return None
        if seq < self._last_seq:
            # Le daemon a redémarré : sa numérotation repart de 1. Sans ça,
            # toutes les demandes suivantes seraient ignorées en silence
            # jusqu'à ce que le compteur repasse au-dessus de l'ancien.
            self._last_seq = seq - 1
        if seq == self._last_seq:
            return None
        self._last_seq = seq
        if now - ts > self._max_age_s:
            return None
        return {"kind": kind, "seq": seq, "ts": ts}


class Gsr2UiRequests:
    """Exécute les demandes du daemon avec ce que le layout principal lui prête."""

    def __init__(
        self,
        *,
        open_panel: Callable[[], None],
        close_panel: Callable[[], None],
        panel_open: Callable[[], bool],
        set_dark: Callable[[bool], None],
        is_dark: Callable[[], bool],
        touched: Callable[[], bool],
        ignition: Callable[[], bool],
        watcher: UiRequestWatcher | None = None,
    ) -> None:
        self._open_panel = open_panel
        self._close_panel = close_panel
        self._panel_open = panel_open
        self._set_dark = set_dark
        self._is_dark = is_dark
        self._touched = touched
        self._ignition = ignition
        self._watcher = watcher or UiRequestWatcher()
        self._was_ignition = True
        self.handled: int = 0

    def poll(self, now: float | None = None) -> str | None:
        """À appeler à chaque image. Rend le nom de l'action exécutée, ou None.

        Une demande dont l'exécution lève est perdue, pas rejouée : la boucle
        de rendu passe ici vingt fois par seconde, et réessayer une action qui
        échoue la ferait échouer vingt fois par seconde.
        """
        try:
            return self._poll(now)
        except Exception:  # noqa: BLE001 — jamais dans la boucle de rendu
            return None

    def _poll(self, now: float | None) -> str | None:
        ignition = bool(self._ignition())
        # Écran noir : n'importe quel toucher rallume, la coupure du contact aussi
        # (on ne laisse pas un écran noir surprendre au prochain démarrage).
        if self._is_dark() and (self._touched() or (self._was_ignition and not ignition)):
            self._set_dark(False)
        self._was_ignition = ignition

        req = self._watcher.poll(now)
        if req is None:
            return None
        kind = req["kind"]
        if kind == KIND_SCREEN_OFF:
            # Une seconde demande rallume : deux appuis pour éteindre, deux pour voir.
            self._set_dark(not self._is_dark())
        elif kind == KIND_PANEL_TOGGLE:
            self._set_dark(False)
            if self._panel_open():
                self._close_panel()
            else:
                self._open_panel()
        else:
            return None
        self.handled += 1
        return kind
