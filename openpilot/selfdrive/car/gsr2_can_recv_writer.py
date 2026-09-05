"""
Writer de trames CAN reçues pour la vue CAN Live (CANviz) de la PWA.

Ce module est copié sur le comma dans /data/openpilot/selfdrive/car/.
Il est appelé par card.py (via le patch 04-gsr2-injection) pour échantillonner
les frames CAN reçues et les écrire dans /tmp/gsr2_can_recv.jsonl.

Le daemon ioniq-control tail ce fichier pour alimenter le LiveStream
exposé via /api/canlive/* dans la PWA.

CONTRAINTES :
- JAMAIS bloquer card.py — toute exception est silencieuse, pas d'IO sync long
- Sampling per-(addr,bus) à 20Hz max pour éviter de saturer tmpfs et la table UI
- Rotation auto à ~5MB pour borner le fichier (tmpfs limité)
- Disable propre si le fichier devient inaccessible
"""

from __future__ import annotations

import json
import os
import time

LIVE_FILE = "/tmp/gsr2_can_recv.jsonl"

# Sampling : intervalle minimum entre deux échantillons d'une même paire (addr, bus)
# 50ms = 20Hz max par signal. Suffisant pour visualisation humaine.
_MIN_INTERVAL_S = 0.05

# Rotation : si le fichier dépasse cette taille, on le tronque en gardant la fin
_MAX_FILE_BYTES = 5 * 1024 * 1024  # 5 MB
_KEEP_TAIL_BYTES = 1 * 1024 * 1024  # garde 1 MB après rotation

# État interne
_last_ts: dict[tuple[int, int], float] = {}
_size_check_counter = 0
_disabled = False


def write_recv_frames(can_list) -> None:
    """Échantillonne et écrit les frames CAN reçues.

    Args:
        can_list: format `list[tuple[nanos, list[tuple[addr, data_bytes, bus]]]]`
                  (sortie de can_capnp_to_list)
    """
    global _size_check_counter, _disabled

    if _disabled or not can_list:
        return

    try:
        now = time.monotonic()
        wall_ts = time.time()
        lines = []

        for entry in can_list:
            # entry = (nanos, [(addr, data, bus), ...])
            try:
                _, frames = entry
            except (ValueError, TypeError):
                continue

            for frame in frames:
                try:
                    addr, data, bus = frame[0], frame[1], frame[2]
                except (IndexError, TypeError):
                    continue

                key = (addr, bus)
                last = _last_ts.get(key, 0.0)
                if now - last < _MIN_INTERVAL_S:
                    continue
                _last_ts[key] = now

                # Format compatible avec LiveStream.feed côté daemon
                line = json.dumps(
                    {
                        "ts": round(wall_ts, 4),
                        "addr": int(addr),
                        "bus": int(bus),
                        "data": bytes(data).hex(),
                    },
                    ensure_ascii=False,
                )
                lines.append(line)

        if not lines:
            return

        with open(LIVE_FILE, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

        # Vérification de taille : 1 fois sur 200 appels (~2s à 100Hz)
        _size_check_counter += 1
        if _size_check_counter >= 200:
            _size_check_counter = 0
            _maybe_rotate()

    except Exception:
        # Toute erreur d'IO ou autre : on désactive proprement et on ne touche plus à rien
        _disabled = True


def _maybe_rotate() -> None:
    """Tronque le fichier si trop gros, en gardant la fin."""
    try:
        size = os.path.getsize(LIVE_FILE)
    except OSError:
        return

    if size <= _MAX_FILE_BYTES:
        return

    try:
        with open(LIVE_FILE, "rb") as f:
            f.seek(-_KEEP_TAIL_BYTES, 2)
            tail = f.read()
        # On coupe au prochain newline pour éviter de garder une ligne tronquée
        nl = tail.find(b"\n")
        if nl >= 0:
            tail = tail[nl + 1 :]
        # Réécriture atomique tmp + rename
        tmp = LIVE_FILE + ".tmp"
        with open(tmp, "wb") as f:
            f.write(tail)
        os.replace(tmp, LIVE_FILE)
    except OSError:
        pass
