"""
Panel "IONIQ Control" pour la UI sunnypilot (Settings).

Affiche l'état du daemon ioniq-control (process séparé tournant en parallèle
de sunnypilot) lu depuis /tmp/gsr2_state.json, plus un QR code d'auto-login
vers la PWA.

Le panel est read-mostly : lecture IPC + 1 action (capture manuelle).
Tous les autres réglages restent dans la PWA (téléphone).

Ce fichier est installé dans selfdrive/ui/layouts/settings/ par le
patch 07-ioniq-control-panel du projet ioniq-control. Ne pas éditer ici.
"""

from __future__ import annotations

import json
import os
import time

import pyray as rl

from openpilot.system.ui.lib.application import FontWeight, MousePos, gui_app
from openpilot.system.ui.lib.multilang import tr
from openpilot.system.ui.widgets import Widget

# ─── Chemins IPC ─────────────────────────────────────────────────
STATE_FILE = "/tmp/gsr2_state.json"
COMMAND_FILE = "/tmp/gsr2_command.json"
QR_PATH = "/data/ioniq-control/qr.png"
STATE_STALE_S = 10.0
CAPTURE_FEEDBACK_S = 10

# ─── Couleurs ────────────────────────────────────────────────────
COLOR_ONLINE = rl.Color(74, 222, 128, 255)
COLOR_DEGRADED = rl.Color(251, 191, 36, 255)
COLOR_OFFLINE = rl.Color(248, 113, 113, 255)
COLOR_TEXT_NORMAL = rl.Color(248, 250, 252, 255)
COLOR_TEXT_DIM = rl.Color(148, 163, 184, 255)
COLOR_BUTTON_BG = rl.Color(34, 211, 238, 255)
COLOR_BUTTON_BG_PRESSED = rl.Color(14, 165, 233, 255)
COLOR_BUTTON_TEXT = rl.Color(15, 23, 42, 255)
COLOR_BUTTON_BG_BUSY = rl.Color(71, 85, 105, 255)

# ─── Dimensions ──────────────────────────────────────────────────
TITLE_SIZE = 65
LABEL_SIZE = 50
VALUE_SIZE = 50
SMALL_SIZE = 38
DOT_RADIUS = 14
BUTTON_HEIGHT = 110
QR_DRAW_SIZE = 480
LINE_SPACING = 70


class IoniqControlLayout(Widget):
    """Panel des Settings sunnypilot affichant l'état du daemon ioniq-control."""

    def __init__(self) -> None:
        super().__init__()
        self._font_med = gui_app.font(FontWeight.MEDIUM)
        self._font_bold = gui_app.font(FontWeight.BOLD)

        self._state: dict | None = None
        self._state_mtime: float = 0.0
        self._state_age: float = 999.0

        self._qr_texture = None
        self._qr_mtime: float = 0.0
        self._qr_failed: bool = False

        self._button_rect = rl.Rectangle(0, 0, 0, 0)
        self._button_pressed = False
        self._capture_sent_at: float = 0.0

    # ── Lifecycle ────────────────────────────────────────────────

    def _update_state(self) -> None:
        self._reload_state()
        self._reload_qr()

    def _reload_state(self) -> None:
        try:
            st = os.stat(STATE_FILE)
            if st.st_mtime != self._state_mtime:
                with open(STATE_FILE) as f:
                    self._state = json.load(f)
                self._state_mtime = st.st_mtime
            self._state_age = max(0.0, time.time() - st.st_mtime)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            self._state = None
            self._state_age = 999.0

    def _reload_qr(self) -> None:
        if self._qr_failed:
            return
        try:
            mtime = os.stat(QR_PATH).st_mtime
        except OSError:
            return
        if mtime == self._qr_mtime and self._qr_texture is not None:
            return
        try:
            img = rl.load_image(QR_PATH)
            tex = rl.load_texture_from_image(img)
            rl.unload_image(img)
            if self._qr_texture is not None:
                rl.unload_texture(self._qr_texture)
            self._qr_texture = tex
            self._qr_mtime = mtime
        except Exception:
            self._qr_failed = True

    # ── Render ───────────────────────────────────────────────────

    def _render(self, rect: rl.Rectangle) -> None:
        x = rect.x
        y = rect.y + 10

        # Title
        rl.draw_text_ex(
            self._font_bold,
            "IONIQ Control",
            rl.Vector2(x, y),
            TITLE_SIZE,
            0,
            COLOR_TEXT_NORMAL,
        )
        version_text = self._fmt_version()
        if version_text:
            vsize = rl.measure_text_ex(self._font_med, version_text, SMALL_SIZE, 0)
            rl.draw_text_ex(
                self._font_med,
                version_text,
                rl.Vector2(rect.x + rect.width - vsize.x, y + 18),
                SMALL_SIZE,
                0,
                COLOR_TEXT_DIM,
            )

        y += TITLE_SIZE + 30
        rl.draw_line_ex(
            rl.Vector2(x, y), rl.Vector2(rect.x + rect.width, y), 2, COLOR_TEXT_DIM
        )
        y += 30

        # Status block (left half)
        left_width = rect.width - QR_DRAW_SIZE - 80
        self._draw_status_block(x, y, left_width)

        # QR block (right side)
        qr_x = rect.x + rect.width - QR_DRAW_SIZE
        self._draw_qr_block(qr_x, y, QR_DRAW_SIZE)

        # Button (bottom)
        button_y = rect.y + rect.height - BUTTON_HEIGHT - 20
        self._button_rect = rl.Rectangle(x, button_y, rect.width, BUTTON_HEIGHT)
        self._draw_button(self._button_rect)

    def _draw_status_block(self, x: float, y: float, width: float) -> None:
        # Daemon health
        health, color = self._health_color()
        self._draw_row(x, y, "Daemon", health, color, dot=True)

        # Ignition
        y += LINE_SPACING
        ign_label = "ON" if self._state and self._state.get("ignition") else "OFF"
        ign_color = COLOR_ONLINE if ign_label == "ON" else COLOR_TEXT_DIM
        self._draw_row(x, y, "Ignition", ign_label, ign_color, dot=False)

        # Uptime
        y += LINE_SPACING
        self._draw_row(x, y, "Uptime", self._fmt_uptime(), COLOR_TEXT_NORMAL, dot=False)

        # Dernière injection
        y += LINE_SPACING
        last = self._fmt_last_disable()
        last_color = COLOR_ONLINE if last.startswith("success") else COLOR_TEXT_DIM
        if last.startswith("error"):
            last_color = COLOR_OFFLINE
        self._draw_row(x, y, "Dernière inj.", last, last_color, dot=False)

        # Config active
        y += LINE_SPACING
        self._draw_row(x, y, "Config", self._fmt_config(), COLOR_TEXT_NORMAL, dot=False)

    def _draw_row(
        self,
        x: float,
        y: float,
        label: str,
        value: str,
        value_color: rl.Color,
        dot: bool,
    ) -> None:
        cx = x + DOT_RADIUS
        if dot:
            rl.draw_circle(int(cx), int(y + LABEL_SIZE / 2), DOT_RADIUS, value_color)
        label_x = cx + (DOT_RADIUS + 15 if dot else -DOT_RADIUS)
        rl.draw_text_ex(
            self._font_med,
            label,
            rl.Vector2(label_x, y),
            LABEL_SIZE,
            0,
            COLOR_TEXT_DIM,
        )
        # Valeur alignée à droite à 320px du label_x
        rl.draw_text_ex(
            self._font_bold,
            value,
            rl.Vector2(label_x + 340, y),
            VALUE_SIZE,
            0,
            value_color,
        )

    def _draw_qr_block(self, x: float, y: float, size: float) -> None:
        if self._qr_texture is None:
            # Placeholder
            rl.draw_rectangle_lines_ex(
                rl.Rectangle(x, y, size, size), 3, COLOR_TEXT_DIM
            )
            msg = tr("QR indisponible")
            tsize = rl.measure_text_ex(self._font_med, msg, SMALL_SIZE, 0)
            rl.draw_text_ex(
                self._font_med,
                msg,
                rl.Vector2(x + (size - tsize.x) / 2, y + size / 2 - SMALL_SIZE / 2),
                SMALL_SIZE,
                0,
                COLOR_TEXT_DIM,
            )
            return
        rl.draw_texture_pro(
            self._qr_texture,
            rl.Rectangle(0, 0, self._qr_texture.width, self._qr_texture.height),
            rl.Rectangle(x, y, size, size),
            rl.Vector2(0, 0),
            0,
            rl.WHITE,
        )
        # Caption sous le QR
        caption = tr("Scan → PWA gsr2.local:8082")
        csize = rl.measure_text_ex(self._font_med, caption, SMALL_SIZE, 0)
        rl.draw_text_ex(
            self._font_med,
            caption,
            rl.Vector2(x + (size - csize.x) / 2, y + size + 12),
            SMALL_SIZE,
            0,
            COLOR_TEXT_DIM,
        )

    def _draw_button(self, rect: rl.Rectangle) -> None:
        busy = self._capture_busy()
        if busy:
            bg = COLOR_BUTTON_BG_BUSY
            text = tr("Capture en cours…")
        elif self._button_pressed:
            bg = COLOR_BUTTON_BG_PRESSED
            text = tr("Capture manuelle (10 s)")
        else:
            bg = COLOR_BUTTON_BG
            text = tr("Capture manuelle (10 s)")
        rl.draw_rectangle_rounded(rect, 0.2, 20, bg)
        tsize = rl.measure_text_ex(self._font_bold, text, LABEL_SIZE, 0)
        rl.draw_text_ex(
            self._font_bold,
            text,
            rl.Vector2(
                rect.x + (rect.width - tsize.x) / 2,
                rect.y + (rect.height - tsize.y) / 2,
            ),
            LABEL_SIZE,
            0,
            COLOR_BUTTON_TEXT if not busy else COLOR_TEXT_NORMAL,
        )

    # ── Mouse ────────────────────────────────────────────────────

    def _handle_mouse_press(self, mouse_pos: MousePos) -> None:
        if rl.check_collision_point_rec(mouse_pos, self._button_rect):
            self._button_pressed = True

    def _handle_mouse_release(self, mouse_pos: MousePos) -> None:
        was_pressed = self._button_pressed
        self._button_pressed = False
        if not was_pressed:
            return
        if not rl.check_collision_point_rec(mouse_pos, self._button_rect):
            return
        if self._capture_busy():
            return
        self._send_capture_command()

    # ── Helpers ──────────────────────────────────────────────────

    def _health_color(self) -> tuple[str, rl.Color]:
        if self._state is None or self._state_age > STATE_STALE_S:
            return tr("offline"), COLOR_OFFLINE
        return tr("online"), COLOR_ONLINE

    def _fmt_uptime(self) -> str:
        if self._state is None:
            return "—"
        s = int(self._state.get("uptime_s", 0))
        h, rem = divmod(s, 3600)
        m = rem // 60
        return f"{h:02d}h {m:02d}m"

    def _fmt_version(self) -> str:
        if self._state is None:
            return ""
        v = self._state.get("version", "")
        return f"v{v}" if v else ""

    def _fmt_last_disable(self) -> str:
        if self._state is None:
            return "—"
        ts = self._state.get("last_disable")
        result = self._state.get("last_disable_result", "")
        if not ts:
            return "—"
        # Format ISO → garder juste HH:MM:SS
        try:
            time_part = ts.split("T", 1)[1][:8]
        except (IndexError, AttributeError):
            time_part = ts
        if result.startswith("error"):
            return f"error @ {time_part}"
        return f"{result or 'ok'} @ {time_part}"

    def _fmt_config(self) -> str:
        if self._state is None:
            return "—"
        cfg = self._state.get("config_snapshot", {})
        isa = "✓" if cfg.get("disable_isa") else "✗"
        hud = "✓" if cfg.get("suppress_hud_speed") else "✗"
        return f"ISA {isa}  HUD {hud}"

    def _capture_busy(self) -> bool:
        return (time.time() - self._capture_sent_at) < CAPTURE_FEEDBACK_S

    def _send_capture_command(self) -> None:
        payload = {
            "type": "capture_start",
            "data": {"max_duration": 10, "deduplicate": True},
            "ts": time.time(),
        }
        tmp = COMMAND_FILE + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(payload, f)
            os.replace(tmp, COMMAND_FILE)
            self._capture_sent_at = time.time()
        except OSError:
            pass
