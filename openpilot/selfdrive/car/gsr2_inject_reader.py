"""
Lecteur de trames GSR2 pour injection dans card.py de sunnypilot.

Ce module est copié sur le comma dans /data/openpilot/selfdrive/car/.
Il est importé par card.py (via le patch) pour lire les trames CAN
écrites par le daemon GSR2 dans /tmp/gsr2_inject.json.

Le daemon écrit, card.py lit et supprime. Communication one-way.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

INJECT_FILE = Path("/tmp/gsr2_inject.json")
_CONSUMED_FILE = Path("/tmp/gsr2_inject_consumed.json")

# Trames périmées après 2 secondes (sécurité si le daemon crash)
MAX_AGE_S = 2.0


def read_inject_frames() -> list[tuple[int, bytes, int]]:
    """Lit et consomme les trames d'injection GSR2.

    Retourne une liste de tuples (address, data, bus) compatibles
    avec le format can_sends de card.py.

    Utilise os.rename() atomique pour déplacer le fichier avant lecture.
    Cela élimine la race condition TOCTOU entre read() et unlink() :
    le daemon peut écrire un nouveau fichier pendant qu'on lit l'ancien.
    Retourne [] si pas de fichier, fichier corrompu, ou trames périmées.
    """
    # Déplacer atomiquement le fichier avant lecture — les nouvelles
    # écritures du daemon iront dans un nouveau fichier inject
    try:
        os.rename(INJECT_FILE, _CONSUMED_FILE)
    except FileNotFoundError:
        return []
    except OSError:
        return []

    try:
        raw = _CONSUMED_FILE.read_bytes()
    except OSError:
        return []
    finally:
        # Nettoyer le fichier consommé dans tous les cas
        try:
            os.unlink(_CONSUMED_FILE)
        except OSError:
            pass

    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return []

    # Vérifier la fraîcheur
    import time
    ts = payload.get("ts", 0)
    if time.time() - ts > MAX_AGE_S:
        return []

    # Convertir en tuples (addr, data_bytes, bus)
    frames = []
    for frame in payload.get("frames", []):
        try:
            addr = int(frame["addr"])
            data = bytes.fromhex(frame["data"])
            bus = int(frame["bus"])
            frames.append((addr, data, bus))
        except (KeyError, ValueError, TypeError):
            continue

    return frames
