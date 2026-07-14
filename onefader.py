# -*- coding: utf-8 -*-
"""
OneFader — פיידר אחד. שליטה חלקה בווליום של המחשב.
MVP ל-Windows: pywebview (UI) + pycaw (Windows Core Audio).

הרצה:      python onefader.py
דרישות:    pip install pywebview pycaw comtypes

© 2026 Avi Berger Productions. All rights reserved.
"""

import os
import sys
import io
import json
import time
import base64
import socket
import secrets
import threading

import math
import webview


class SmoothedVolume:
    """החלקת פרמטר כמו בקונסולה דיגיטלית: היעד קופץ, הווליום גולש אליו
    בקבוע זמן של ~45ms. מוחק מדרגות שמיעה בגרירה, ברימוט ובפיידים —
    זה מה שנותן ל'פיידר על מסך' תחושת פיידר אמיתי באוזניים."""

    TICK = 0.005   # 200Hz
    TC = 0.045     # קבוע זמן — קצר מספיק שלא מרגישים לג, ארוך מספיק להחליק

    def __init__(self, raw):
        self.raw = raw
        self.FAST = getattr(raw, "FAST", False)
        start = raw.get()
        self._target = start
        self._current = start
        self._alpha = 1.0 - math.exp(-self.TICK / self.TC)
        self._wake = threading.Event()
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            d = self._target - self._current
            if abs(d) < 0.0005:
                self._current = self._target
                try:
                    self.raw.set(self._current)   # נעילה על הערך הסופי המדויק
                except Exception:
                    pass
                self._wake.wait()      # לישון עד שמגיע יעד חדש
                self._wake.clear()
                continue
            self._current += d * self._alpha
            try:
                self.raw.set(self._current)
            except Exception:
                pass                   # התקן פלט התחלף באמצע — נתפוס בטיק הבא
            time.sleep(self.TICK)

    def set(self, level):
        self._target = max(0.0, min(1.0, float(level)))
        self._wake.set()

    def get(self) -> float:
        return self.raw.get()

    def is_muted(self) -> bool:
        return self.raw.is_muted()

    def settings(self):
        return self.raw.settings()

# ---------- שליטת ווליום — Windows או macOS ----------

class WindowsVolume:
    """Master volume של Windows דרך pycaw."""

    def __init__(self):
        from ctypes import POINTER, cast
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

        devices = AudioUtilities.GetSpeakers()
        # Newer pycaw returns an AudioDevice wrapper instead of the raw IMMDevice;
        # the COM interface we need (.Activate) lives on the inner device.
        if not hasattr(devices, "Activate"):
            devices = (getattr(devices, "_dev", None)
                       or getattr(devices, "dev", None)
                       or getattr(devices, "_device", None)
                       or devices)
        interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        self._volume = cast(interface, POINTER(IAudioEndpointVolume))

    def get(self) -> float:
        return float(self._volume.GetMasterVolumeLevelScalar())

    def set(self, level: float):
        level = max(0.0, min(1.0, float(level)))
        self._volume.SetMasterVolumeLevelScalar(level, None)
        if level > 0 and self._volume.GetMute():
            self._volume.SetMute(0, None)

    def is_muted(self) -> bool:
        return bool(self._volume.GetMute())

    def settings(self):
        return self.get(), self.is_muted()


class MacCoreAudioVolume:
    """ווליום דרך CoreAudio ישירות — מיידי (מיקרו-שניות) וברזולוציית float.
    בלי osascript, בלי subprocess, בלי 100 מדרגות. זה מה שמאפשר
    גרירה בריל-טיים ופיידים חלקים בלי מדרגות שמיעה."""

    FAST = True  # מאפשר ל-FadeEngine לרוץ ב-50Hz

    def __init__(self):
        import ctypes
        self._ct = ctypes
        self._ca = ctypes.CDLL(
            "/System/Library/Frameworks/CoreAudio.framework/CoreAudio"
        )

        class PropAddr(ctypes.Structure):
            _fields_ = [
                ("selector", ctypes.c_uint32),
                ("scope", ctypes.c_uint32),
                ("element", ctypes.c_uint32),
            ]

        self._PropAddr = PropAddr
        fourcc = lambda s: (ord(s[0]) << 24) | (ord(s[1]) << 16) | (ord(s[2]) << 8) | ord(s[3])
        self._kDefaultOut  = fourcc("dOut")  # default output device
        self._kScopeGlobal = fourcc("glob")
        self._kScopeOutput = fourcc("outp")
        self._kVolume      = fourcc("vmvc")  # virtual master volume
        self._kMute        = fourcc("mute")
        # בדיקת שפיות בעלייה — אם נכשל, make_system_volume ייפול ל-osascript
        self.get()

    def _device(self):
        """ה-AudioDeviceID של התקן הפלט הנוכחי (נשלף כל פעם —
        המשתמש יכול להחליף לאוזניות באמצע)."""
        ct = self._ct
        addr = self._PropAddr(self._kDefaultOut, self._kScopeGlobal, 0)
        dev = ct.c_uint32(0)
        size = ct.c_uint32(ct.sizeof(dev))
        err = self._ca.AudioObjectGetPropertyData(
            1, ct.byref(addr), 0, None, ct.byref(size), ct.byref(dev)
        )
        if err or dev.value == 0:
            raise OSError(f"CoreAudio: no default output device (err={err})")
        return dev.value

    def get(self) -> float:
        ct = self._ct
        addr = self._PropAddr(self._kVolume, self._kScopeOutput, 0)
        val = ct.c_float(0.0)
        size = ct.c_uint32(ct.sizeof(val))
        err = self._ca.AudioObjectGetPropertyData(
            self._device(), ct.byref(addr), 0, None, ct.byref(size), ct.byref(val)
        )
        if err:
            raise OSError(f"CoreAudio: get volume failed (err={err})")
        return max(0.0, min(1.0, float(val.value)))

    def set(self, level: float):
        ct = self._ct
        level = max(0.0, min(1.0, float(level)))
        dev = self._device()
        addr = self._PropAddr(self._kVolume, self._kScopeOutput, 0)
        val = ct.c_float(level)
        err = self._ca.AudioObjectSetPropertyData(
            dev, ct.byref(addr), 0, None, ct.sizeof(val), ct.byref(val)
        )
        if err:
            raise OSError(f"CoreAudio: set volume failed (err={err})")
        if level > 0 and self._get_mute(dev):
            self._set_mute(dev, False)

    def _get_mute(self, dev) -> bool:
        ct = self._ct
        addr = self._PropAddr(self._kMute, self._kScopeOutput, 0)
        val = ct.c_uint32(0)
        size = ct.c_uint32(ct.sizeof(val))
        err = self._ca.AudioObjectGetPropertyData(
            dev, ct.byref(addr), 0, None, ct.byref(size), ct.byref(val)
        )
        return (not err) and bool(val.value)

    def _set_mute(self, dev, muted: bool):
        ct = self._ct
        addr = self._PropAddr(self._kMute, self._kScopeOutput, 0)
        val = ct.c_uint32(1 if muted else 0)
        self._ca.AudioObjectSetPropertyData(
            dev, ct.byref(addr), 0, None, ct.sizeof(val), ct.byref(val)
        )

    def is_muted(self) -> bool:
        return self._get_mute(self._device())

    def settings(self):
        return self.get(), self.is_muted()


class MacVolume:
    """Output volume של macOS דרך osascript (סקאלת 0–100 של המערכת).
    גיבוי בלבד — איטי. בשימוש רק אם CoreAudio לא זמין."""

    def __init__(self):
        import subprocess
        self._run = lambda script: subprocess.run(
            ["osascript", "-e", script], capture_output=True, text=True
        ).stdout.strip()
        self._last_set = None
        # בדיקת שפיות אחת בעלייה — שהגישה לווליום עובדת
        self.get()

    def get(self) -> float:
        out = self._run("output volume of (get volume settings)")
        try:
            return int(out) / 100.0
        except ValueError:
            return 0.0

    def set(self, level: float):
        v = round(max(0.0, min(1.0, float(level))) * 100)
        if v == self._last_set:
            return  # למק יש רק 100 מדרגות — לא שווה קריאת מערכת על אותו ערך
        self._last_set = v
        self._run(f"set volume output volume {v}")
        if v > 0 and self.is_muted():
            self._run("set volume without output muted")

    def is_muted(self) -> bool:
        return self._run("output muted of (get volume settings)") == "true"

    def settings(self):
        """רמה + מיוט בקריאת מערכת אחת (osascript יקר, חבל על שתיים)."""
        out = self._run("get volume settings")  # "output volume:44, ..., output muted:false"
        level, muted = 0.0, False
        for part in out.split(", "):
            if part.startswith("output volume:"):
                try:
                    level = int(part.split(":")[1]) / 100.0
                except ValueError:
                    pass
            elif part.startswith("output muted:"):
                muted = part.split(":")[1] == "true"
        return level, muted


def make_system_volume():
    if sys.platform == "darwin":
        try:
            return MacCoreAudioVolume()   # מהיר — הדרך הנכונה
        except Exception:
            return MacVolume()            # גיבוי איטי דרך osascript
    if sys.platform == "win32":
        return WindowsVolume()
    raise RuntimeError("OneFader supports Windows and macOS")


# ---------- מנוע הפייד ----------

def _smoothstep(t: float) -> float:
    """עקומת S חלקה — מתחילה ונגמרת בעדינות, כמו יד של סאונדמן."""
    return t * t * (3.0 - 2.0 * t)


class FadeEngine:
    """פייד ברקע עם אפשרות ביטול. שומר את הרמה האחרונה לפייד-אין."""

    def __init__(self, vol):
        self.vol = vol
        # CoreAudio/pycaw מהירים → 50Hz לפייד חלק. osascript איטי → 20Hz.
        self.STEP_SEC = 0.02 if getattr(vol, "FAST", sys.platform == "win32") else 0.05
        self._thread = None
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self.state = "idle"  # idle | fading_in | fading_out
        # רמת היעד לפייד-אין: הרמה שהמשתמש עבד איתה לאחרונה
        current = self.vol.get()
        self.restore_level = current if current > 0.05 else 0.8

    def remember_level(self, level: float):
        if level > 0.05:
            self.restore_level = level

    def stop(self):
        self._cancel.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=0.2)
        self.state = "idle"

    def fade_to(self, target: float, duration: float):
        with self._lock:
            self.stop()
            self._cancel = threading.Event()
            self.state = "fading_out" if target < self.vol.get() else "fading_in"
            self._thread = threading.Thread(
                target=self._run, args=(target, duration, self._cancel), daemon=True
            )
            self._thread.start()

    def _run(self, target: float, duration: float, cancel: threading.Event):
        start = self.vol.get()
        if abs(target - start) < 0.001 or duration <= 0:
            self.vol.set(target)
            self.state = "idle"
            return
        t0 = time.perf_counter()
        while not cancel.is_set():
            elapsed = time.perf_counter() - t0
            t = min(1.0, elapsed / duration)
            self.vol.set(start + (target - start) * _smoothstep(t))
            if t >= 1.0:
                break
            time.sleep(self.STEP_SEC)
        if not cancel.is_set():
            self.state = "idle"


# ---------- MIDI: שלט/פיידר פיזי שולט באפליקציה ----------

class MidiManager:
    """קלט MIDI עם MIDI Learn. שלושה תפקידים: volume (פיידר/נוב, CC),
    fade_in ו-fade_out (כפתורים, Note או CC). החיבורים נשמרים לקובץ
    כך שהשלט זכור גם אחרי הפעלה מחדש. תומך בחיבור/ניתוק תוך כדי ריצה."""

    SLOTS = ("volume", "fade_in", "fade_out")
    CONFIG = os.path.join(os.path.expanduser("~"), ".onefader-midi.json")

    def __init__(self, api):
        self.api = api
        self.available = False   # python-rtmidi מותקן?
        self.error = None
        self.ports = []          # שמות הכניסות הפתוחות
        self.learn_slot = None   # תפקיד שממתין ללמידה
        self.last_event = None   # אירוע אחרון שנקלט (להצגה ב-UI)
        self.bindings = {}       # slot -> {"type": "cc"|"note", "ch": int, "num": int, "port": str}
        self._ins = {}           # port_name -> MidiIn
        self._btn_state = {}     # slot -> last value (לזיהוי לחיצה ולא החזקה)
        self._load()
        try:
            import rtmidi  # noqa: F401 — בדיקת זמינות בלבד
            self.available = True
        except Exception as e:
            self.error = f"python-rtmidi not installed ({e})"
            return
        threading.Thread(target=self._watch_ports, daemon=True).start()

    # --- persistence ---
    def _load(self):
        try:
            with open(self.CONFIG, encoding="utf-8") as f:
                data = json.load(f)
            self.bindings = {k: v for k, v in data.get("bindings", {}).items()
                             if k in self.SLOTS}
        except Exception:
            self.bindings = {}

    def _save(self):
        try:
            with open(self.CONFIG, "w", encoding="utf-8") as f:
                json.dump({"bindings": self.bindings}, f, indent=2)
        except Exception:
            pass  # דיסק לקריאה בלבד וכו' — לא קריטי

    # --- ports (hot-plug) ---
    def _watch_ports(self):
        import rtmidi
        probe = rtmidi.MidiIn()
        while True:
            try:
                names = probe.get_ports()
                # כניסות חדשות
                for i, name in enumerate(names):
                    if name not in self._ins:
                        mi = rtmidi.MidiIn()
                        mi.open_port(i)
                        mi.set_callback(self._make_callback(name))
                        self._ins[name] = mi
                # כניסות שנותקו
                for name in list(self._ins):
                    if name not in names:
                        try:
                            self._ins[name].close_port()
                        except Exception:
                            pass
                        del self._ins[name]
                self.ports = list(self._ins.keys())
            except Exception:
                pass  # rtmidi זרק באמצע רענון התקנים — ננסה שוב בסיבוב הבא
            time.sleep(2)

    # --- input handling ---
    def _make_callback(self, port_name):
        def cb(event, _data=None):
            try:
                self._handle(port_name, event[0])
            except Exception:
                pass  # הודעה חריגה לא תפיל את האפליקציה
        return cb

    def _handle(self, port, msg):
        if len(msg) < 3:
            return
        status, num, val = msg[0], msg[1], msg[2]
        kind = status & 0xF0
        ch = status & 0x0F
        if kind == 0xB0:
            etype = "cc"
        elif kind == 0x90 and val > 0:
            etype = "note"
        else:
            return
        self.last_event = {"type": etype, "ch": ch, "num": num, "val": val, "port": port}

        # --- MIDI Learn ---
        if self.learn_slot:
            slot = self.learn_slot
            if slot == "volume" and etype != "cc":
                return  # ווליום חייב פיידר/נוב (CC), לא כפתור
            self.bindings[slot] = {"type": etype, "ch": ch, "num": num, "port": port}
            self.learn_slot = None
            self._save()
            return

        # --- normal operation ---
        for slot, b in self.bindings.items():
            if b["type"] != etype or b["ch"] != ch or b["num"] != num:
                continue
            if slot == "volume":
                self.api.set_level(val / 127.0)
            else:
                # כפתור: מגיבים לעליית ערך בלבד (לא להחזקה/שחרור)
                prev = self._btn_state.get(slot, 0)
                self._btn_state[slot] = val
                pressed = (etype == "note") or (val >= 64 > prev)
                if pressed:
                    if slot == "fade_in":
                        self.api.fade_in(self.api.fade_seconds)
                    else:
                        self.api.fade_out(self.api.fade_seconds)

    # --- API for the UI ---
    def info(self):
        return {
            "available": self.available,
            "error": self.error,
            "ports": self.ports,
            "bindings": self.bindings,
            "learning": self.learn_slot,
            "lastEvent": self.last_event,
        }

    def learn(self, slot):
        if slot in self.SLOTS:
            self.learn_slot = slot
        return self.info()

    def cancel_learn(self):
        self.learn_slot = None
        return self.info()

    def clear(self, slot):
        self.bindings.pop(slot, None)
        self._btn_state.pop(slot, None)
        self._save()
        return self.info()


# ---------- Remote: שליטה מהטלפון דרך הדפדפן ----------

def _local_ip() -> str:
    """ה-IP של המחשב ברשת המקומית (בשביל ה-QR)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def _resource(name: str) -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


class RemoteServer:
    """מרים HTTP (דף הטלפון) + WebSocket (פקודות וסטטוס) על הרשת המקומית."""

    HTTP_PORT = 1780
    WS_PORT = 1781

    def __init__(self, api):
        self.api = api
        self.pin = None
        self.running = False
        self.clients = set()          # חיבורי WS מאומתים
        self._loop = None

    def start(self):
        if not self.running:
            self.pin = f"{secrets.randbelow(10000):04d}"
            self.running = True
            threading.Thread(target=self._http_thread, daemon=True).start()
            threading.Thread(target=self._ws_thread, daemon=True).start()
        return self.info()

    def info(self):
        url = f"http://{_local_ip()}:{self.HTTP_PORT}"
        return {
            "running": self.running,
            "url": url,
            "pin": self.pin,
            "qr": self._qr_datauri(url) if self.running else None,
            "connected": len(self.clients),
        }

    def _qr_datauri(self, url: str) -> str:
        import qrcode
        img = qrcode.make(url)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()

    # --- HTTP: מגיש את remote.html לכל טלפון ברשת ---
    def _http_thread(self):
        import http.server
        with open(_resource("remote.html"), encoding="utf-8") as f:
            html = f.read().replace("{{WS_PORT}}", str(self.WS_PORT)).encode("utf-8")

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(html)
            def log_message(self, *a):
                pass

        http.server.ThreadingHTTPServer(("0.0.0.0", self.HTTP_PORT), Handler).serve_forever()

    # --- WebSocket: פקודות מהטלפון, סטטוס לכל המחוברים ---
    def _ws_thread(self):
        import asyncio
        import logging
        import websockets
        logging.getLogger("websockets").setLevel(logging.CRITICAL)

        async def handler(ws):
            authed = False
            try:
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except ValueError:
                        continue
                    if msg.get("type") == "hello":
                        if str(msg.get("pin", "")) == self.pin:
                            authed = True
                            self.clients.add(ws)
                            await ws.send(json.dumps({"type": "ok", **self.api.status()}))
                        else:
                            await ws.send(json.dumps({"type": "badpin"}))
                    elif authed:
                        t = msg.get("type")
                        if t == "set":
                            self.api.set_level(msg.get("level", 0))
                        elif t == "fade":
                            self.api.fade_to(msg.get("target", 0), msg.get("seconds", 5))
                        elif t == "stop":
                            self.api.stop_fade()
                        elif t == "fadeSeconds":
                            self.api.set_fade_seconds(msg.get("seconds", 5))
            except websockets.exceptions.ConnectionClosed:
                pass  # טלפון נעל מסך / יצא מהדף — צפוי, יתחבר מחדש לבד
            finally:
                self.clients.discard(ws)

        async def broadcaster():
            while True:
                if self.clients:
                    payload = json.dumps({"type": "status", **self.api.status()})
                    import asyncio as aio
                    await aio.gather(
                        *[c.send(payload) for c in list(self.clients)],
                        return_exceptions=True,
                    )
                await asyncio.sleep(0.05)  # 20Hz — CoreAudio זול, הסטטוס כמעט חי

        async def main():
            async with websockets.serve(handler, "0.0.0.0", self.WS_PORT):
                await broadcaster()

        asyncio.run(main())


# ---------- API שנחשף ל-UI ----------

class Api:
    def __init__(self):
        self.vol = SmoothedVolume(make_system_volume())
        self.fade_seconds = 5.0   # state משותף — כל המסכים מסונכרנים אליו
        self.engine = FadeEngine(self.vol)
        self.remote = RemoteServer(self)
        self.midi = MidiManager(self)

    # --- MIDI (שלט פיזי) ---
    def midi_info(self):
        return self.midi.info()

    def midi_learn(self, slot):
        return self.midi.learn(slot)

    def midi_cancel_learn(self):
        return self.midi.cancel_learn()

    def midi_clear(self, slot):
        return self.midi.clear(slot)

    def remote_start(self):
        """מדליק את השליטה מהטלפון ומחזיר URL + PIN + QR."""
        return self.remote.start()

    def remote_info(self):
        return self.remote.info()

    def status(self):
        level, muted = self.vol.settings()
        return {
            "level": level,
            "muted": muted,
            "state": self.engine.state,
            "restore": self.engine.restore_level,
            "fadeSeconds": self.fade_seconds,
        }

    def set_fade_seconds(self, seconds):
        """בחירת זמן פייד מכל מסך — מתעדכנת בכולם דרך הסטטוס."""
        s = float(seconds)
        if 0.5 <= s <= 60:
            self.fade_seconds = s
        return self.status()

    def set_level(self, level):
        """גרירה ידנית של הפיידר — מבטלת פייד פעיל."""
        self.engine.stop()
        self.vol.set(level)
        self.engine.remember_level(float(level))
        return self.status()

    def fade_to(self, target, seconds):
        """פייד לרמת יעד כלשהי — ה-UI קובע את היעד, הקונסולה מבצעת."""
        target = max(0.0, min(1.0, float(target)))
        if target == 0.0:
            self.engine.remember_level(self.vol.get())
        else:
            self.engine.remember_level(target)
        self.engine.fade_to(target, float(seconds))
        return self.status()

    def fade_in(self, seconds):
        self.engine.fade_to(self.engine.restore_level, float(seconds))
        return self.status()

    def fade_out(self, seconds):
        self.engine.remember_level(self.vol.get())
        self.engine.fade_to(0.0, float(seconds))
        return self.status()

    def stop_fade(self):
        self.engine.stop()
        return self.status()


# ---------- טעינת ה-UI (תומך גם ב-exe של PyInstaller) ----------

def ui_path() -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "ui.html")


def main():
    api = Api()
    backend = type(api.vol.raw).__name__
    if backend == "MacCoreAudioVolume":
        engine_desc = "CoreAudio (fast) + smoothing"
    elif backend == "WindowsVolume":
        engine_desc = "pycaw (fast) + smoothing"
    else:
        engine_desc = "osascript (SLOW FALLBACK — CoreAudio failed!)"
    print(f"OneFader v2.2 | audio engine: {engine_desc}", flush=True)
    webview.create_window(
        "OneFader",
        url=ui_path(),
        js_api=api,
        width=400,
        height=860,
        min_size=(340, 640),
        background_color="#0C0E12",
    )
    webview.start()


if __name__ == "__main__":
    main()
