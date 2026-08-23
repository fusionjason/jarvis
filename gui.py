"""PyQt6 desktop UI for Jarvis, styled after the dark cyan/amber "HUD" theme
used in the Mark XXXIX-OR project (github.com/FatihMakes/Mark-XXXIX-OR).

Two threading rules this file exists to satisfy:
  1. Qt widgets may only be touched from the main (GUI) thread.
  2. Jarvis.send() blocks on the network for seconds at a time.
So each message runs on a ChatWorker (QThread), and tool confirmation prompts
cross back to the main thread via ConfirmBridge, which blocks the worker
thread on a threading.Event until the user answers a dialog.
"""

import math
import random
import sys
import threading
import time
from datetime import datetime
from typing import Optional

import psutil

from PyQt6.QtCore import QObject, QPointF, QRectF, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QFont, QPainter, QPen, QTextCursor
from PyQt6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QPushButton, QSizePolicy, QTextEdit, QVBoxLayout, QWidget,
)

import tools
from config import ASSISTANT_NAME, MODEL
from jarvis import Jarvis

FONT = "Courier New"


class C:
    """Color palette, adapted from Mark XXXIX-OR's arc-reactor HUD theme."""
    BG = "#00060a"
    PANEL = "#010d14"
    PANEL2 = "#010f18"
    BORDER = "#0d3347"
    BORDER_A = "#0f4060"
    BORDER_B = "#1a5c7a"
    PRI = "#00d4ff"       # cyan — Jarvis / headings
    PRI_DIM = "#007a99"
    PRI_GHO = "#001f2e"   # faint cyan — background grid dots, outer halo rings
    ACC = "#ffcc00"       # amber — user / accents
    GREEN = "#00ff88"     # status-online
    RED = "#ff4d4d"       # errors
    BAR_BG = "#04222e"    # gauge track background
    TEXT = "#cfeff7"
    TEXT_DIM = "#5b8a99"


STYLESHEET = f"""
QMainWindow, QWidget {{ background: {C.BG}; }}
QLabel {{ color: {C.TEXT}; background: transparent; }}
QTextEdit {{
    background: {C.PANEL}; color: {C.TEXT}; border: 1px solid {C.BORDER};
    border-radius: 4px; padding: 6px; selection-background-color: {C.PRI_DIM};
}}
QLineEdit {{
    background: {C.PANEL2}; color: {C.TEXT}; border: 1px solid {C.BORDER_B};
    border-radius: 4px; padding: 8px; font-size: 12pt;
}}
QLineEdit:focus {{ border: 1px solid {C.PRI}; }}
QPushButton {{
    background: {C.PANEL2}; color: {C.PRI}; border: 1px solid {C.BORDER_B};
    border-radius: 4px; padding: 8px 18px; font-weight: bold;
}}
QPushButton:hover {{ border: 1px solid {C.PRI}; color: {C.BG}; background: {C.PRI}; }}
QPushButton:disabled {{ color: {C.TEXT_DIM}; border-color: {C.BORDER}; background: {C.PANEL}; }}
QPushButton:checked {{ background: {C.PRI}; color: {C.BG}; border: 1px solid {C.PRI}; }}
QScrollBar:vertical {{ background: {C.PANEL}; width: 10px; }}
QScrollBar::handle:vertical {{ background: {C.BORDER_B}; border-radius: 4px; min-height: 24px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; }}
QMessageBox {{ background: {C.PANEL}; }}
QMessageBox QLabel {{ color: {C.TEXT}; font-family: '{FONT}'; }}
"""


def qcol(hex_str: str, alpha: int = 255) -> QColor:
    c = QColor(hex_str)
    c.setAlpha(max(0, min(255, alpha)))
    return c


class MetricBar(QWidget):
    """A small labeled gauge: name on the left, value text on the right, a thin fill bar below —
    used for the live CPU/memory/disk/network readouts in the system panel.
    """

    def __init__(self, label: str, parent=None):
        super().__init__(parent)
        self._label = label
        self._value = 0.0  # 0-100; bar fill and color threshold
        self._text = "--"
        self.setFixedHeight(38)
        self.setMinimumWidth(80)

    def set_value(self, pct: float, text: str) -> None:
        self._value = max(0.0, min(100.0, pct))
        self._text = text
        self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()

        p.setBrush(QBrush(qcol(C.PANEL2)))
        p.setPen(QPen(qcol(C.BORDER_A), 1))
        p.drawRoundedRect(QRectF(1, 1, w - 2, h - 2), 4, 4)

        bar_h, bar_x = 4, 6
        bar_y = h - bar_h - 5
        bar_w = w - 12
        fill_w = int(bar_w * self._value / 100)

        p.setBrush(QBrush(qcol(C.BAR_BG)))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(QRectF(bar_x, bar_y, bar_w, bar_h), 2, 2)

        if self._value > 85:
            bar_col = qcol(C.RED)
        elif self._value > 65:
            bar_col = qcol(C.ACC)
        else:
            bar_col = qcol(C.PRI)
        if fill_w > 0:
            p.setBrush(QBrush(bar_col))
            p.drawRoundedRect(QRectF(bar_x, bar_y, fill_w, bar_h), 2, 2)

        p.setFont(QFont(FONT, 7, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.TEXT_DIM), 1))
        p.drawText(QRectF(8, 5, 50, 14), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self._label)

        p.setFont(QFont(FONT, 9, QFont.Weight.Bold))
        p.setPen(QPen(bar_col if self._text != "--" else qcol(C.TEXT_DIM), 1))
        p.drawText(QRectF(0, 4, w - 6, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, self._text)


class HudCanvas(QWidget):
    """The animated circular centerpiece — glowing rings, scanning arcs, tick marks, a crosshair,
    and a status readout, all custom-drawn and driven by a QTimer. Reacts to whatever state
    MainWindow is in (idle/listening/thinking/speaking) via set_state().

    Adapted from Mark XXXIX-OR's HudCanvas (github.com/FatihMakes/Mark-XXXIX-OR) — simplified
    (no face image, no mute state) and re-driven off Jarvis's own state model instead of theirs.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(260, 260)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self._state = "idle"       # idle | listening | thinking | speaking
        self._status_text = "ONLINE"
        self._status_color = C.GREEN

        self._tick = 0
        self._halo = 55.0
        self._tgt_halo = 55.0
        self._rings = [0.0, 120.0, 240.0]
        self._scan = 0.0
        self._scan2 = 180.0
        self._pulses: list[float] = []
        self._particles: list[list[float]] = []
        self._blink = True
        self._blink_tick = 0

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._step)
        self._timer.start(33)  # ~30fps — plenty smooth for this, lighter than 16ms/60fps

    def set_state(self, status_text: str, status_color: str, state: str = "idle") -> None:
        self._status_text = status_text
        self._status_color = status_color
        self._state = state

    def _step(self) -> None:
        self._tick += 1
        speaking = self._state == "speaking"
        active = self._state != "idle"

        self._tgt_halo = random.uniform(140, 190) if speaking else (
            random.uniform(70, 95) if active else random.uniform(45, 65)
        )
        self._halo += (self._tgt_halo - self._halo) * (0.35 if speaking else 0.15)

        speeds = [1.3, -0.9, 2.0] if speaking else ([0.8, -0.55, 1.2] if active else [0.4, -0.25, 0.65])
        for i, spd in enumerate(speeds):
            self._rings[i] = (self._rings[i] + spd) % 360
        self._scan = (self._scan + (3.0 if speaking else 1.3)) % 360
        self._scan2 = (self._scan2 + (-2.0 if speaking else -0.75)) % 360

        fw = min(self.width(), self.height())
        lim = fw * 0.74
        pulse_spd = 4.2 if speaking else 2.0
        self._pulses = [r + pulse_spd for r in self._pulses if r + pulse_spd < lim]
        if len(self._pulses) < 3 and random.random() < (0.07 if active else 0.02):
            self._pulses.append(0.0)

        if speaking and random.random() < 0.28:
            cx, cy = self.width() / 2, self.height() / 2
            ang = random.uniform(0, 2 * math.pi)
            r_s = fw * 0.28
            self._particles.append([
                cx + math.cos(ang) * r_s, cy + math.sin(ang) * r_s,
                math.cos(ang) * random.uniform(0.9, 2.4),
                math.sin(ang) * random.uniform(0.9, 2.4) - 0.4, 1.0,
            ])
        self._particles = [
            [p[0] + p[2], p[1] + p[3], p[2] * 0.97, p[3] * 0.97, p[4] - 0.028]
            for p in self._particles if p[4] > 0
        ]

        self._blink_tick += 1
        if self._blink_tick >= 20:
            self._blink = not self._blink
            self._blink_tick = 0
        self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), qcol(C.BG))

        w, h = self.width(), self.height()
        cx, cy = w / 2, h / 2
        fw = min(w, h)
        col = qcol(self._status_color)

        # background grid dots
        p.setPen(QPen(qcol(C.PRI_GHO), 1))
        for x in range(0, w, 48):
            for y in range(0, h, 48):
                p.drawPoint(x, y)

        r_face = fw * 0.31

        # halo glow
        for i in range(10):
            r = r_face * (1.8 - i * 0.08)
            frc = 1.0 - i / 10
            a = max(0, min(255, int(self._halo * 0.085 * frc)))
            p.setPen(QPen(qcol(self._status_color, a), 1.5))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QRectF(cx - r, cy - r, r * 2, r * 2))

        # outward pulse rings
        for pr in self._pulses:
            a = max(0, int(200 * (1.0 - pr / (fw * 0.74))))
            p.setPen(QPen(qcol(self._status_color, a), 1.5))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QRectF(cx - pr, cy - pr, pr * 2, pr * 2))

        # spinning arc rings
        for idx, (r_frac, w_r, arc_l, gap) in enumerate(
            [(0.48, 3, 115, 78), (0.40, 2, 78, 55), (0.32, 1, 56, 40)]
        ):
            ring_r = fw * r_frac
            base = self._rings[idx]
            a_val = max(0, min(255, int(self._halo * (1.0 - idx * 0.18))))
            p.setPen(QPen(qcol(self._status_color, a_val), w_r))
            p.setBrush(Qt.BrushStyle.NoBrush)
            angle = base
            rect = QRectF(cx - ring_r, cy - ring_r, ring_r * 2, ring_r * 2)
            while angle < base + 360:
                p.drawArc(rect, int(angle * 16), int(arc_l * 16))
                angle += arc_l + gap

        # scanning arcs
        sr = fw * 0.50
        sa = min(255, int(self._halo * 1.5))
        ex = 75 if self._state == "speaking" else 44
        srect = QRectF(cx - sr, cy - sr, sr * 2, sr * 2)
        p.setPen(QPen(qcol(self._status_color, sa), 2.5))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawArc(srect, int(self._scan * 16), int(ex * 16))
        p.setPen(QPen(qcol(C.ACC, sa // 2), 1.5))
        p.drawArc(srect, int(self._scan2 * 16), int(ex * 16))

        # tick marks
        t_out, t_in = fw * 0.497, fw * 0.474
        p.setPen(QPen(qcol(C.PRI, 140), 1))
        for deg in range(0, 360, 10):
            rad = math.radians(deg)
            inn = t_in if deg % 30 == 0 else t_in + 6
            p.drawLine(
                QPointF(cx + t_out * math.cos(rad), cy - t_out * math.sin(rad)),
                QPointF(cx + inn * math.cos(rad), cy - inn * math.sin(rad)),
            )

        # crosshair
        ch_r, gap_h = fw * 0.51, fw * 0.16
        p.setPen(QPen(qcol(C.PRI, int(self._halo * 0.5)), 1))
        p.drawLine(QPointF(cx - ch_r, cy), QPointF(cx - gap_h, cy))
        p.drawLine(QPointF(cx + gap_h, cy), QPointF(cx + ch_r, cy))
        p.drawLine(QPointF(cx, cy - ch_r), QPointF(cx, cy - gap_h))
        p.drawLine(QPointF(cx, cy + gap_h), QPointF(cx, cy + ch_r))

        # corner brackets
        bl = 24
        bc = qcol(C.PRI, 210)
        hl, hr = cx - fw / 2, cx + fw / 2
        ht, hb = cy - fw / 2, cy + fw / 2
        p.setPen(QPen(bc, 2))
        for bx, by, dx, dy in [(hl, ht, 1, 1), (hr, ht, -1, 1), (hl, hb, 1, -1), (hr, hb, -1, -1)]:
            p.drawLine(QPointF(bx, by), QPointF(bx + dx * bl, by))
            p.drawLine(QPointF(bx, by), QPointF(bx, by + dy * bl))

        # center orb + wordmark (no face image — this is a from-scratch build, not a movie prop)
        orb_r = int(fw * 0.27)
        for i in range(8, 0, -1):
            r2 = int(orb_r * i / 8)
            frc = i / 8
            a = max(0, min(255, int(self._halo * 1.1 * frc)))
            base = QColor(col)
            p.setBrush(QBrush(QColor(int(base.red() * frc), int(base.green() * frc), int(base.blue() * frc), a)))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(QRectF(cx - r2, cy - r2, r2 * 2, r2 * 2))
        p.setPen(QPen(qcol(C.PRI, min(255, int(self._halo * 2))), 1))
        p.setFont(QFont(FONT, 13, QFont.Weight.Bold))
        p.drawText(QRectF(cx - 80, cy - 14, 160, 28), Qt.AlignmentFlag.AlignCenter, ASSISTANT_NAME.upper())

        # particles (speaking only)
        for pt in self._particles:
            a = max(0, min(255, int(pt[4] * 255)))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(qcol(self._status_color, a)))
            p.drawEllipse(QPointF(pt[0], pt[1]), 2.5, 2.5)

        # status text
        sy = cy + fw * 0.40
        sym = "●" if self._blink else "○"
        p.setPen(QPen(col, 1))
        p.setFont(QFont(FONT, 11, QFont.Weight.Bold))
        p.drawText(QRectF(0, sy, w, 26), Qt.AlignmentFlag.AlignCenter, f"{sym}  {self._status_text}")

        # waveform
        wy = sy + 30
        n, bw_ = 36, 8
        wx0 = (w - n * bw_) / 2
        for i in range(n):
            if self._state == "speaking":
                hgt = random.randint(3, 20)
                cl = qcol(self._status_color) if hgt > 12 else qcol(C.PRI_DIM)
            else:
                hgt = int(3 + 2 * math.sin(self._tick * 0.09 + i * 0.6))
                cl = qcol(C.BORDER_B)
            p.fillRect(QRectF(wx0 + i * bw_, wy + 20 - hgt, bw_ - 1, hgt), cl)


class ConfirmBridge(QObject):
    """Lets tools running on a worker thread pop a confirm dialog on the GUI thread
    and block until the user answers — registered with tools.set_confirm_handler.
    """
    _request = pyqtSignal(str, object)

    def __init__(self, parent_window: QWidget):
        super().__init__()
        self._window = parent_window
        self._request.connect(self._handle_request)

    def confirm(self, prompt: str) -> bool:
        event = threading.Event()
        result = {"answer": False}
        self._request.emit(prompt, (event, result))
        event.wait()
        return result["answer"]

    def _handle_request(self, prompt: str, payload) -> None:
        event, result = payload
        box = QMessageBox(self._window)
        box.setWindowTitle(f"{ASSISTANT_NAME} — confirm")
        box.setText(prompt)
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        answer = box.exec()
        result["answer"] = answer == QMessageBox.StandardButton.Yes
        event.set()


class ChatWorker(QThread):
    """Runs one Jarvis.send() call off the GUI thread."""
    tool_used = pyqtSignal(str, dict)
    finished_ok = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, jarvis: Jarvis, user_input: str):
        super().__init__()
        self.jarvis = jarvis
        self.user_input = user_input

    def run(self) -> None:
        try:
            reply = self.jarvis.send(self.user_input, on_tool_use=lambda name, inp: self.tool_used.emit(name, inp))
            self.finished_ok.emit(reply)
        except Exception as e:  # noqa: BLE001 — surface any failure to the UI rather than crash the thread
            self.failed.emit(str(e))


class MicWorker(QThread):
    """Records from the mic and transcribes it locally, off the GUI thread. Imports voice.py
    lazily so the GUI doesn't pay Whisper's load time until the mic is actually used.
    """
    heard = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, max_seconds: Optional[float] = None):
        super().__init__()
        self.max_seconds = max_seconds

    def run(self) -> None:
        try:
            import voice
            kwargs = {} if self.max_seconds is None else {"max_seconds": self.max_seconds}
            text = voice.listen_and_transcribe(**kwargs)
            self.heard.emit(text)
        except Exception as e:  # noqa: BLE001 — surface any failure to the UI rather than crash the thread
            self.failed.emit(str(e))


class WakeWorker(QThread):
    """Listens continuously in the background for the wake word ("Hey Jarvis"), off the GUI
    thread. Emits `detected` once and exits — the caller starts a fresh WakeWorker to keep
    listening after handling the resulting command, rather than looping inside one instance.
    """
    detected = pyqtSignal()
    failed = pyqtSignal(str)

    def __init__(self, stop_event: threading.Event):
        super().__init__()
        self._stop_event = stop_event

    def run(self) -> None:
        try:
            import voice
            heard = voice.wait_for_wake_word(stop_check=self._stop_event.is_set)
            if heard:
                self.detected.emit()
        except Exception as e:  # noqa: BLE001 — surface any failure to the UI rather than crash the thread
            self.failed.emit(str(e))


class SpeakWorker(QThread):
    """Speaks text aloud off the GUI thread, mic left live the whole time — if the user talks
    over the reply, playback stops immediately and `interrupted` fires with what they said
    instead of waiting for `finished`.
    """
    interrupted = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, text: str):
        super().__init__()
        self.text = text

    def run(self) -> None:
        try:
            import voice
            heard = voice.speak_with_barge_in(self.text)
            if heard:
                self.interrupted.emit(heard)
        except Exception as e:  # noqa: BLE001 — surface any failure to the UI rather than crash the thread
            self.failed.emit(str(e))


class MainWindow(QMainWindow):
    def __init__(self, jarvis: Jarvis):
        super().__init__()
        self.jarvis = jarvis
        self._worker: Optional[ChatWorker] = None
        self._mic_worker: Optional[MicWorker] = None
        self._speak_worker: Optional[SpeakWorker] = None
        self._wake_worker: Optional[WakeWorker] = None
        self._wake_stop_event = threading.Event()
        self._voice_triggered = False   # set by _on_heard; consumed by _on_send
        self._reply_should_speak = False  # captured per in-flight request, read by _on_reply
        self._auto_send_after_mic = False  # set by _on_wake_detected; consumed by _on_mic_done
        self._in_wake_conversation = False  # True from wake word until a follow-up hears silence
        self._speak_was_interrupted = False  # set by _on_speak_interrupted; consumed by _on_speak_done
        self._last_net_bytes: Optional[int] = None  # for computing network throughput deltas
        self._last_net_time = 0.0
        self._confirm_bridge = ConfirmBridge(self)
        tools.set_confirm_handler(self._confirm_bridge.confirm)

        self.setWindowTitle(ASSISTANT_NAME.upper())
        self.resize(1200, 760)
        self.setMinimumSize(900, 600)
        self.setStyleSheet(STYLESHEET)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        root.addWidget(self._build_header())

        body = QHBoxLayout()
        body.setSpacing(10)
        body.addWidget(self._build_left_panel(), 0)
        body.addWidget(self._build_center_panel(), 2)
        body.addWidget(self._build_right_panel(), 2)
        root.addLayout(body, 1)

        root.addLayout(self._build_input_row())

        self._clock_timer = QTimer(self)
        self._clock_timer.timeout.connect(self._tick_clock)
        self._clock_timer.start(1000)
        self._tick_clock()

        self._metrics_timer = QTimer(self)
        self._metrics_timer.timeout.connect(self._tick_metrics)
        self._metrics_timer.start(2000)
        self._tick_metrics()

        self._set_status("●  ONLINE", C.GREEN)  # also primes the HUD's initial state
        self._append_chat(ASSISTANT_NAME, f"Online. Model: {MODEL}. How can I help?", C.PRI)

    # -- layout -----------------------------------------------------------

    def _build_header(self) -> QFrame:
        frame = QFrame()
        frame.setStyleSheet(f"background: {C.PANEL}; border: 1px solid {C.BORDER}; border-radius: 4px;")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(14, 8, 14, 8)

        title = QLabel(ASSISTANT_NAME.upper())
        title.setFont(QFont(FONT, 16, QFont.Weight.Bold))
        title.setStyleSheet(f"color: {C.PRI};")
        layout.addWidget(title)

        self._status_lbl = QLabel("●  ONLINE")
        self._status_lbl.setFont(QFont(FONT, 9, QFont.Weight.Bold))
        self._status_lbl.setStyleSheet(f"color: {C.GREEN};")
        layout.addWidget(self._status_lbl)

        layout.addStretch(1)

        self._clock_lbl = QLabel()
        self._clock_lbl.setFont(QFont(FONT, 12, QFont.Weight.Bold))
        self._clock_lbl.setStyleSheet(f"color: {C.PRI};")
        layout.addWidget(self._clock_lbl)

        return frame

    def _build_left_panel(self) -> QFrame:
        frame = QFrame()
        frame.setFixedWidth(240)
        frame.setStyleSheet(f"background: {C.PANEL}; border: 1px solid {C.BORDER}; border-radius: 4px;")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(12, 12, 12, 12)

        info_hdr = QLabel("SYSTEM MONITOR")
        info_hdr.setFont(QFont(FONT, 9, QFont.Weight.Bold))
        info_hdr.setStyleSheet(f"color: {C.PRI_DIM};")
        layout.addWidget(info_hdr)

        self._cpu_bar = MetricBar("CPU")
        self._mem_bar = MetricBar("MEM")
        self._disk_bar = MetricBar("DISK")
        self._net_bar = MetricBar("NET")
        for bar in (self._cpu_bar, self._mem_bar, self._disk_bar, self._net_bar):
            layout.addWidget(bar)

        layout.addSpacing(6)
        model_lbl = QLabel(f"model: {MODEL}")
        model_lbl.setFont(QFont(FONT, 8))
        model_lbl.setStyleSheet(f"color: {C.TEXT_DIM};")
        model_lbl.setWordWrap(True)
        layout.addWidget(model_lbl)

        layout.addSpacing(14)

        tool_hdr = QLabel("TOOL ACTIVITY")
        tool_hdr.setFont(QFont(FONT, 9, QFont.Weight.Bold))
        tool_hdr.setStyleSheet(f"color: {C.PRI_DIM};")
        layout.addWidget(tool_hdr)

        self._tool_log = QTextEdit()
        self._tool_log.setReadOnly(True)
        self._tool_log.setFont(QFont(FONT, 8))
        layout.addWidget(self._tool_log, 1)

        return frame

    def _build_center_panel(self) -> QFrame:
        frame = QFrame()
        frame.setStyleSheet(f"background: {C.PANEL}; border: 1px solid {C.BORDER}; border-radius: 4px;")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(4, 4, 4, 4)

        self._hud = HudCanvas()
        layout.addWidget(self._hud)

        return frame

    def _build_right_panel(self) -> QFrame:
        frame = QFrame()
        frame.setStyleSheet(f"background: {C.PANEL}; border: 1px solid {C.BORDER}; border-radius: 4px;")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(12, 12, 12, 12)

        hdr = QLabel("ACTIVITY LOG")
        hdr.setFont(QFont(FONT, 9, QFont.Weight.Bold))
        hdr.setStyleSheet(f"color: {C.PRI_DIM};")
        layout.addWidget(hdr)

        self._chat_view = QTextEdit()
        self._chat_view.setReadOnly(True)
        self._chat_view.setFont(QFont(FONT, 10))
        layout.addWidget(self._chat_view, 1)

        return frame

    def _build_input_row(self) -> QHBoxLayout:
        input_row = QHBoxLayout()

        self._input = QLineEdit()
        self._input.setPlaceholderText("Type a message…")
        self._input.returnPressed.connect(self._on_send_clicked)
        input_row.addWidget(self._input, 1)

        self._mic_btn = QPushButton("🎤")
        self._mic_btn.setToolTip("Speak instead of typing (local, offline transcription)")
        self._mic_btn.setFixedWidth(44)
        self._mic_btn.clicked.connect(self._on_mic_clicked)
        input_row.addWidget(self._mic_btn)

        self._wake_btn = QPushButton("🎧 WAKE")
        self._wake_btn.setToolTip('Hands-free: say "Hey Jarvis" to start listening, no click needed')
        self._wake_btn.setCheckable(True)
        self._wake_btn.toggled.connect(self._on_wake_toggled)
        input_row.addWidget(self._wake_btn)

        self._send_btn = QPushButton("SEND")
        self._send_btn.clicked.connect(self._on_send_clicked)
        input_row.addWidget(self._send_btn)

        return input_row

    # -- behavior -----------------------------------------------------------

    def _tick_clock(self) -> None:
        self._clock_lbl.setText(datetime.now().strftime("%H:%M:%S"))

    def _tick_metrics(self) -> None:
        cpu = psutil.cpu_percent(interval=None)
        self._cpu_bar.set_value(cpu, f"{cpu:.0f}%")

        mem = psutil.virtual_memory()
        self._mem_bar.set_value(mem.percent, f"{mem.percent:.0f}%")

        disk = psutil.disk_usage("C:\\")
        self._disk_bar.set_value(disk.percent, f"{disk.percent:.0f}%")

        net = psutil.net_io_counters()
        now = time.time()
        total_bytes = net.bytes_sent + net.bytes_recv
        if self._last_net_bytes is not None:
            elapsed = max(now - self._last_net_time, 0.001)
            kbps = (total_bytes - self._last_net_bytes) / 1024 / elapsed
            pct = min(100.0, kbps / 5)  # rough visual scale — 500 KB/s reads as "full"
            self._net_bar.set_value(pct, f"{kbps:.0f}K/s")
        self._last_net_bytes = total_bytes
        self._last_net_time = now

    def _append_chat(self, speaker: str, text: str, color: str) -> None:
        self._chat_view.append(
            f'<span style="color:{color}; font-weight:bold;">{speaker}:</span> '
            f'<span style="color:{C.TEXT};">{text}</span>'
        )
        self._chat_view.moveCursor(QTextCursor.MoveOperation.End)

    def _busy(self) -> bool:
        return self._worker is not None or self._mic_worker is not None or self._speak_worker is not None

    def _on_send_clicked(self) -> None:
        # Only the genuine UI-triggered path (Enter / SEND click) — typing manually means the
        # user has taken over, so any hands-free wake conversation in progress should stop
        # trying to auto-listen for a follow-up afterward.
        self._in_wake_conversation = False
        self._on_send()

    def _on_send(self) -> None:
        text = self._input.text().strip()
        if not text or self._busy():
            return

        self._append_chat("You", text, C.ACC)
        self._input.clear()
        self._set_controls_enabled(False)
        self._set_status("●  THINKING…", C.ACC)

        # Reply gets spoken back only if this message came from the mic — typed messages get
        # typed replies, so a long fast text exchange isn't slowed down by speech playback.
        self._reply_should_speak = self._voice_triggered
        self._voice_triggered = False

        self._worker = ChatWorker(self.jarvis, text)
        self._worker.tool_used.connect(self._on_tool_used)
        self._worker.finished_ok.connect(self._on_reply)
        self._worker.failed.connect(self._on_error)
        self._worker.finished.connect(self._on_chat_done)
        self._worker.start()

    def _on_mic_clicked(self) -> None:
        if self._busy():
            return
        self._stop_wake_listening_if_active()  # free the mic before opening a second stream on it
        self._in_wake_conversation = False  # manual click takes over from any hands-free flow
        self._start_mic_listen(auto_send=False)

    def _start_mic_listen(self, auto_send: bool, max_seconds: Optional[float] = None) -> None:
        self._set_controls_enabled(False)
        self._set_status("●  LISTENING…", C.RED)
        self._auto_send_after_mic = auto_send

        self._mic_worker = MicWorker(max_seconds=max_seconds)
        self._mic_worker.heard.connect(self._on_heard)
        self._mic_worker.failed.connect(self._on_error)
        self._mic_worker.finished.connect(self._on_mic_done)
        self._mic_worker.start()

    def _on_wake_toggled(self, checked: bool) -> None:
        if checked:
            self._maybe_resume_wake_listening()
        else:
            if self._wake_worker is not None:
                self._wake_stop_event.set()
            elif not self._busy():
                self._set_status("●  ONLINE", C.GREEN)

    def _start_wake_worker(self) -> None:
        self._wake_stop_event = threading.Event()
        self._wake_worker = WakeWorker(self._wake_stop_event)
        self._wake_worker.detected.connect(self._on_wake_detected)
        self._wake_worker.failed.connect(self._on_wake_failed)
        self._wake_worker.finished.connect(self._on_wake_worker_finished)
        self._wake_worker.start()
        self._set_status('●  LISTENING FOR "HEY JARVIS"…', C.PRI_DIM)

    def _stop_wake_listening_if_active(self) -> None:
        if self._wake_worker is not None:
            self._wake_stop_event.set()
            self._wake_worker.wait(2000)
            self._wake_worker = None

    def _maybe_resume_wake_listening(self) -> None:
        if self._wake_btn.isChecked() and self._wake_worker is None and not self._busy():
            self._start_wake_worker()

    def _on_wake_detected(self) -> None:
        self._append_chat("mic", '"Hey Jarvis" heard — listening for your request…', C.PRI_DIM)
        self._in_wake_conversation = True
        self._start_mic_listen(auto_send=True)

    def _after_exchange_complete(self) -> None:
        """Called once a full exchange has genuinely finished (chat done and, if applicable,
        spoken). Mid-conversation, this keeps listening for a follow-up without requiring "Hey
        Jarvis" again; otherwise it falls back to (re)waiting for the wake word.
        """
        if self._in_wake_conversation and self._wake_btn.isChecked():
            # Same uncapped listen as the initial "Hey Jarvis" trigger — no rush on follow-ups.
            self._start_mic_listen(auto_send=True)
        else:
            self._in_wake_conversation = False
            self._maybe_resume_wake_listening()

    def _on_wake_failed(self, message: str) -> None:
        self._append_chat("wake word", message, C.RED)
        self._wake_btn.setChecked(False)  # avoid retrying forever against a broken setup

    def _on_wake_worker_finished(self) -> None:
        self._wake_worker = None

    def _on_tool_used(self, name: str, tool_input: dict) -> None:
        self._tool_log.append(f'<span style="color:{C.PRI};">→ {name}</span>({tool_input})')
        self._tool_log.moveCursor(QTextCursor.MoveOperation.End)

    def _on_reply(self, text: str) -> None:
        self._append_chat(ASSISTANT_NAME, text or "(no response)", C.PRI)
        if self._reply_should_speak and text:
            self._speak(text)
        self._reply_should_speak = False

    def _on_error(self, message: str) -> None:
        self._append_chat("error", message, C.RED)

    def _on_heard(self, text: str) -> None:
        # Transcribed text lands in the input box for review rather than auto-sending — mishears
        # shouldn't be able to trigger a confirm-gated tool (run_command, delete, etc.) unedited.
        if not text:
            self._append_chat("mic", "(didn't catch anything)", C.TEXT_DIM)
        else:
            self._input.setText(text)
            self._voice_triggered = True

    def _speak(self, text: str) -> None:
        self._set_controls_enabled(False)
        self._set_status("●  SPEAKING…", C.PRI)
        self._speak_worker = SpeakWorker(text)
        self._speak_worker.interrupted.connect(self._on_speak_interrupted)
        self._speak_worker.failed.connect(self._on_error)
        self._speak_worker.finished.connect(self._on_speak_done)
        self._speak_worker.start()

    def _on_speak_interrupted(self, text: str) -> None:
        """The user talked over a reply — SpeakWorker already stopped playback. Clear it now (the
        thread's own `finished` signal hasn't landed yet, and _on_send()'s busy-check would
        otherwise see it as still active) and immediately treat what they said as the next turn.
        """
        self._speak_worker = None
        self._speak_was_interrupted = True
        self._append_chat("mic", "you cut in — stopping to listen…", C.PRI_DIM)
        self._input.setText(text)
        self._voice_triggered = True
        self._on_send()  # appends the actual transcribed text as a normal "You" bubble itself

    def _on_chat_done(self) -> None:
        self._worker = None
        # _on_reply (connected to finished_ok) always runs before this (connected to finished),
        # so by now self._speak_worker is already set if the reply is about to be spoken —
        # in that case leave controls disabled and let _on_speak_done re-enable them instead.
        if self._speak_worker is None:
            self._set_controls_enabled(True)
            self._set_status("●  ONLINE", C.GREEN)
            self._after_exchange_complete()

    def _on_mic_done(self) -> None:
        self._mic_worker = None
        # Only the wake-word flow auto-sends; the manual mic button still leaves the transcribed
        # text in the box for review (see _on_heard) — an empty box here means nothing was heard.
        if self._auto_send_after_mic:
            self._auto_send_after_mic = False
            if self._input.text().strip():
                self._on_send()  # takes over controls/status itself, and its own completion
                return           # handlers will eventually call _after_exchange_complete()
            # Silence: keep listening rather than dropping back to requiring "Hey Jarvis" again —
            # _in_wake_conversation stays True, so _after_exchange_complete() below re-listens.
        self._set_controls_enabled(True)
        self._set_status("●  ONLINE", C.GREEN)
        self._after_exchange_complete()

    def _on_speak_done(self) -> None:
        self._speak_worker = None
        if self._speak_was_interrupted:
            # _on_speak_interrupted already started the next exchange for what the user cut in
            # with — that exchange owns controls/status now, so there's nothing left to do here.
            self._speak_was_interrupted = False
            return
        self._set_controls_enabled(True)
        self._set_status("●  ONLINE", C.GREEN)
        self._after_exchange_complete()

    def _set_controls_enabled(self, enabled: bool) -> None:
        self._input.setEnabled(enabled)
        self._send_btn.setEnabled(enabled)
        self._mic_btn.setEnabled(enabled)
        if enabled:
            self._input.setFocus()

    _HUD_STATE_BY_COLOR = {
        C.GREEN: "idle",
        C.ACC: "thinking",
        C.RED: "listening",
        C.PRI_DIM: "listening",
        C.PRI: "speaking",
    }

    def _set_status(self, text: str, color: str) -> None:
        self._status_lbl.setText(text)
        self._status_lbl.setStyleSheet(f"color: {color};")
        self._hud.set_state(text, color, self._HUD_STATE_BY_COLOR.get(color, "idle"))


def main() -> None:
    app = QApplication(sys.argv)

    try:
        jarvis = Jarvis()
    except SystemExit as e:
        QMessageBox.critical(None, f"{ASSISTANT_NAME} — setup needed", str(e))
        sys.exit(1)

    window = MainWindow(jarvis)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
