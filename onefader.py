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

    FAST = True  # pycaw מהיר — פיידים ב-50Hz

    def __init__(self):
        from ctypes import POINTER, cast
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

        self._com_ready = threading.local()
        self._ensure_com()
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

    def _ensure_com(self):
        """COM חייב אתחול פר-thread. הקריאות מגיעות מכל מיני threads —
        ההחלקה, הפייד, גשר ה-JS, הרימוט וה-MIDI — לכן מאתחלים עצלנית בכל אחד."""
        if getattr(self._com_ready, "done", False):
            return
        try:
            import comtypes
            comtypes.CoInitialize()
        except Exception:
            pass  # כבר מאותחל ב-thread הזה — בסדר גמור
        self._com_ready.done = True

    def get(self) -> float:
        self._ensure_com()
        return float(self._volume.GetMasterVolumeLevelScalar())

    def set(self, level: float):
        self._ensure_com()
        level = max(0.0, min(1.0, float(level)))
        self._volume.SetMasterVolumeLevelScalar(level, None)
        if level > 0 and self._volume.GetMute():
            self._volume.SetMute(0, None)

    def is_muted(self) -> bool:
        self._ensure_com()
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
        # דה-דופ קצר בלבד: מגן מספאם osascript בזמן החלקה, אבל פג אחרי 2ש'
        # כדי שערך שהשתנה חיצונית (מקשי ווליום) לא ייתקע על המטמון לנצח.
        now = time.time()
        if v == self._last_set and now - getattr(self, "_last_set_t", 0) < 2.0:
            return
        self._last_set = v
        self._last_set_t = now
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

    def _stop_locked(self):
        self._cancel.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=0.5)
        self.state = "idle"

    def stop(self):
        # תמיד תחת נעילה — אחרת stop מ-thread אחד (טלפון/MIDI) יכול להצטלב
        # עם fade_to מ-thread אחר, לבטל את האירוע הלא-נכון ולהשאיר פייד רץ.
        with self._lock:
            self._stop_locked()

    def fade_to(self, target: float, duration: float):
        with self._lock:
            self._stop_locked()
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


# ---------- רישוי: תשתית Free/Pro (שקטה — בעידן ההשקה הכל פתוח) ----------

PRO_FEATURES = ("remote", "midi", "ride")   # מה ייסגר מאחורי Pro כשנפעיל חנות


class License:
    """מפתח רישיון מקומי. FOUNDER_ERA=True ⇒ הכל פתוח לכולם (עידן ההשקה).
    כשתקום חנות (Lemon Squeezy/Gumroad): FOUNDER_ERA=False + אימות מפתח
    מול ה-API של המנפיק ב-save()."""

    FOUNDER_ERA = True
    PATH = os.path.join(os.path.expanduser("~"), ".onefader-license.json")

    def __init__(self):
        self.key = None
        self.email = None
        self._load()

    def _load(self):
        try:
            with open(self.PATH, encoding="utf-8") as f:
                d = json.load(f)
            self.key = d.get("key")
            self.email = d.get("email")
        except Exception:
            pass

    def save(self, key, email=None):
        self.key, self.email = key, email
        try:
            with open(self.PATH, "w", encoding="utf-8") as f:
                json.dump({"key": key, "email": email}, f, indent=2)
        except Exception:
            pass

    def is_pro(self) -> bool:
        if self.FOUNDER_ERA:
            return True
        return bool(self.key)   # כשנדליק: כאן ייכנס אימות אמיתי של המפתח

    def allows(self, feature: str) -> bool:
        return feature not in PRO_FEATURES or self.is_pro()


# ---------- Auto-Ride: מד עוצמת שמע + בקר AGC ----------

def _amp_db(x: float) -> float:
    return 20.0 * math.log10(max(1e-6, x))


class WindowsLoopbackMeter:
    """WASAPI loopback — מודד את מה שבאמת יוצא לרמקולים (אחרי הווליום),
    ולכן הבקר עובד במשוב סגור. בלי הרשאות, בלי דרייברים."""

    POST_GAIN = True   # המדידה כוללת את הווליום שלנו → בקרת משוב

    def __init__(self):
        import pyaudiowpatch  # ImportError → הפיצ'ר לא זמין, נקי
        self._pa_mod = pyaudiowpatch
        self.rms_db = None          # None = אין מדידה תקפה כרגע
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self.rms_db = None

    def _loop(self):
        import struct
        pa = self._pa_mod.PyAudio()
        try:
            while not self._stop.is_set():
                try:
                    info = pa.get_default_wasapi_loopback()
                    rate = int(info["defaultSampleRate"])
                    ch = max(1, int(info["maxInputChannels"]))
                    frames = max(256, rate // 10)   # חלונות של ~100ms
                    st = pa.open(format=self._pa_mod.paFloat32, channels=ch,
                                 rate=rate, input=True,
                                 input_device_index=info["index"],
                                 frames_per_buffer=frames)
                    while not self._stop.is_set():
                        data = st.read(frames, exception_on_overflow=False)
                        n = len(data) // 4
                        if n:
                            vals = struct.unpack(f"<{n}f", data)
                            acc = 0.0
                            for v in vals:
                                acc += v * v
                            self.rms_db = _amp_db((acc / n) ** 0.5)
                    st.close()
                except Exception:
                    # התקן פלט התחלף / נעלם — מנסים לפתוח מחדש עוד שנייה
                    self.rms_db = None
                    if self._stop.wait(1.0):
                        break
        finally:
            pa.terminate()


class MacSystemTapMeter:
    """Core Audio process tap (macOS 14.2+) — מודד את סכום האודיו של כל
    התהליכים לפני ווליום ההתקן → הבקר עובד בהזנה-קדימה (feedforward).
    דורש אישור חד-פעמי של System Audio Recording."""

    POST_GAIN = False

    def __init__(self):
        import ctypes
        import objc  # מגיע עם pywebview במק
        self._ct = ctypes
        CATapDescription = objc.lookUpClass("CATapDescription")  # ימות על מק ישן
        self._ca = ctypes.CDLL(
            "/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
        desc = CATapDescription.alloc().initStereoGlobalTapButExcludeProcesses_([])
        desc.setName_("OneFader AutoRide meter")
        tap_id = ctypes.c_uint32(0)
        err = self._ca.AudioHardwareCreateProcessTap(
            ctypes.c_void_p(objc.pyobjc_id(desc)), ctypes.byref(tap_id))
        if err or not tap_id.value:
            raise OSError(f"process tap failed (err={err}) — permission denied?")
        self._tap_id = tap_id.value
        self._desc = desc          # מחזיקים חי — אחרת ה-tap נהרס
        self._agg_id = None
        self._ioproc = None
        self.rms_db = None
        self._make_aggregate(objc)

    def _make_aggregate(self, objc):
        """התקן aggregate פרטי שמכיל רק את ה-tap — ממנו קוראים את הדגימות."""
        import ctypes
        import uuid
        from Foundation import NSDictionary
        tap_uuid = self._desc.UUID().UUIDString()
        cfg = NSDictionary.dictionaryWithDictionary_({
            "uid": str(uuid.uuid4()),
            "name": "OneFader AutoRide",
            "private": True,
            "taps": [{"uid": str(tap_uuid), "drift": True}],
        })
        agg = ctypes.c_uint32(0)
        err = self._ca.AudioHardwareCreateAggregateDevice(
            ctypes.c_void_p(objc.pyobjc_id(cfg)), ctypes.byref(agg))
        if err or not agg.value:
            raise OSError(f"aggregate device failed (err={err})")
        self._agg_id = agg.value

    def start(self):
        import ctypes
        ct = ctypes
        IOProc = ct.CFUNCTYPE(ct.c_int32, ct.c_uint32, ct.c_void_p, ct.c_void_p,
                              ct.c_void_p, ct.c_void_p, ct.c_void_p, ct.c_void_p)
        meter = self

        def ioproc(dev, now, in_data, in_time, out_data, out_time, ctx):
            try:
                if in_data:
                    # AudioBufferList: UInt32 mNumberBuffers; ואז buffers
                    nbuf = ct.cast(in_data, ct.POINTER(ct.c_uint32))[0]
                    if nbuf:
                        # AudioBuffer: UInt32 mNumberChannels, UInt32 mDataByteSize, void* mData
                        base = in_data + 4 + 4  # padding מיושר ל-8 על arm64
                        hdr = ct.cast(base, ct.POINTER(ct.c_uint32))
                        nbytes = hdr[1]
                        dptr = ct.cast(base + 8, ct.POINTER(ct.c_void_p))[0]
                        n = min(nbytes // 4, 4096)
                        if dptr and n:
                            vals = ct.cast(dptr, ct.POINTER(ct.c_float * n))[0]
                            acc = 0.0
                            for v in vals:
                                acc += v * v
                            meter.rms_db = _amp_db((acc / n) ** 0.5)
            except Exception:
                pass
            return 0

        self._ioproc_cb = IOProc(ioproc)   # reference חי — אחרת ה-GC הורג את ה-callback
        proc_id = ct.c_void_p(0)
        err = self._ca.AudioDeviceCreateIOProcID(
            self._agg_id, self._ioproc_cb, None, ct.byref(proc_id))
        if err:
            raise OSError(f"IOProc failed (err={err})")
        self._ioproc = proc_id
        err = self._ca.AudioDeviceStart(self._agg_id, proc_id)
        if err:
            raise OSError(f"AudioDeviceStart failed (err={err})")

    def stop(self):
        try:
            if self._ioproc:
                self._ca.AudioDeviceStop(self._agg_id, self._ioproc)
                self._ca.AudioDeviceDestroyIOProcID(self._agg_id, self._ioproc)
                self._ioproc = None
            if self._agg_id:
                self._ca.AudioHardwareDestroyAggregateDevice(self._agg_id)
                self._agg_id = None
            if self._tap_id:
                self._ca.AudioHardwareDestroyProcessTap(self._tap_id)
                self._tap_id = None
        except Exception:
            pass
        self.rms_db = None


def make_meter():
    if sys.platform == "win32":
        return WindowsLoopbackMeter()
    if sys.platform == "darwin":
        # הצנרת עובדת, אבל macOS משתיק את ה-tap לאפליקציה לא חתומה (אין ייחוס
        # TCC) — עד שנחתום ב-Developer ID, עדיף הודעה כנה מכפתור שמאזין לנצח.
        if not os.environ.get("ONEFADER_MAC_RIDE"):
            raise OSError("Auto-Ride is coming to Mac soon (Windows has it today)")
        return MacSystemTapMeter()
    raise OSError("no meter for this platform")


class AutoRide:
    """מחזיק את העוצמה הנשמעת קבועה: נועל יעד ברגע ההפעלה ורוכב על
    הפיידר כדי לפצות על שירים חזקים/חלשים. שקט (מעבר בין שירים) מקפיא
    את הרכיבה כדי לא להרים ווליום בין שירים."""

    GATE_DB = -55.0     # מתחת לזה = שקט, מקפיאים
    RANGE_DB = 12.0     # תקרת פיצוי סביב נקודת ההפעלה
    TICK = 0.1
    EMA_TC = 0.8        # החלקת המדידה — שלא נגיב לכל תוף

    def __init__(self, api):
        self.api = api
        self.active = False
        self.state = "off"          # off | listening | locked
        self.error = None
        self.meter = None
        self._stop = threading.Event()
        self._thread = None
        self._ema = None
        self._target = None
        self._anchor_scalar = None  # הווליום בנקודת הנעילה
        self._relock_at = 0.0

    # --- lifecycle ---
    def toggle(self):
        return self.disengage() if self.active else self.engage()

    def engage(self):
        try:
            self.meter = make_meter()
            self.meter.start()
        except Exception as e:
            self.meter = None
            self.error = str(e) or "audio meter unavailable"
            return self.info()
        self.error = None
        self.active = True
        self.state = "listening"
        self._ema = None
        self._target = None
        self._anchor_scalar = self.api.vol.get()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self.info()

    def disengage(self):
        self.active = False
        self.state = "off"
        self._stop.set()
        if self.meter:
            self.meter.stop()
            self.meter = None
        return self.info()

    def notify_manual(self):
        """המשתמש גרר ידנית באמצע רכיבה — הרמה החדשה היא היעד החדש."""
        if self.active:
            self.state = "listening"
            self._target = None
            self._relock_at = time.time() + 1.2   # לתת להחלקה להתייצב

    def info(self):
        return {"active": self.active, "state": self.state, "error": self.error}

    # --- the ride itself ---
    def _loop(self):
        alpha = 1.0 - math.exp(-self.TICK / self.EMA_TC)
        while not self._stop.wait(self.TICK):
            m = self.meter.rms_db if self.meter else None
            if m is None or m < self.GATE_DB:
                continue                       # שקט/אין מדידה — מקפיאים הכל
            self._ema = m if self._ema is None else self._ema + alpha * (m - self._ema)
            now = time.time()
            if self._target is None:
                if now >= self._relock_at:
                    self._target = self._ema
                    self._anchor_scalar = self.api.vol.get()
                    self.state = "locked"
                continue
            if self.api.engine.state != "idle":
                continue                       # פייד ידני מנצח — לא נלחמים בו
            err_db = self._target - self._ema
            if abs(err_db) < 0.8:
                continue                       # דד-בנד — יציבות בלי ריצודים
            cur = self.api.vol.get()
            if getattr(self.meter, "POST_GAIN", True):
                # משוב סגור: צעדים קטנים עד שהמדידה חוזרת ליעד
                step = max(-0.02, min(0.02, err_db * 0.004))
                new = cur + step
            else:
                # הזנה-קדימה: המדידה לא כוללת את הווליום — מפצים ישירות
                new = self._anchor_scalar * (10.0 ** (err_db / 20.0))
                new = cur + max(-0.02, min(0.02, new - cur))   # מגביל סלו
            lo = self._anchor_scalar * (10.0 ** (-self.RANGE_DB / 20.0))
            hi = min(1.0, self._anchor_scalar * (10.0 ** (self.RANGE_DB / 20.0)))
            new = max(lo, min(hi, new))
            if abs(new - cur) >= 0.001:
                self.api.vol.set(new)
                self.api.engine.remember_level(new)


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
        self._bind_lock = threading.Lock()   # callbacks של פורטים שונים רצים במקביל
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
                        # ההתקנים יכלו להשתנות בין get_ports לפתיחה — מאמתים שהאינדקס
                        # עדיין מצביע על אותו התקן, אחרת האירועים ישויכו לפורט הלא נכון
                        cur = mi.get_ports()
                        if i >= len(cur) or cur[i] != name:
                            continue  # הרשימה השתנתה — נתפוס בסיבוב הבא
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
            with self._bind_lock:
                self.bindings[slot] = {"type": etype, "ch": ch, "num": num, "port": port}
                self.learn_slot = None
            self._save()
            return

        # --- normal operation ---
        with self._bind_lock:
            snapshot = list(self.bindings.items())
        for slot, b in snapshot:
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
        with self._bind_lock:
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
        self.error = None
        self.clients = set()          # חיבורי WS מאומתים
        self._loop = None

    def _ports_free(self) -> bool:
        """בדיקה שהפורטים פנויים לפני שמכריזים שהשרת רץ — אחרת מופע שני
        של האפליקציה מציג QR ו-PIN לשרת שבכלל שייך למופע הראשון."""
        for port in (self.HTTP_PORT, self.WS_PORT):
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                probe.bind(("0.0.0.0", port))
            except OSError:
                return False
            finally:
                probe.close()
        return True

    def start(self):
        if not self.running:
            if not self._ports_free():
                self.error = "Ports 1780/1781 are busy — is another OneFader already running?"
                return self.info()
            self.error = None
            self.pin = f"{secrets.randbelow(10000):04d}"
            self.running = True
            threading.Thread(target=self._http_thread, daemon=True).start()
            threading.Thread(target=self._ws_thread, daemon=True).start()
        return self.info()

    def info(self):
        url = f"http://{_local_ip()}:{self.HTTP_PORT}"
        return {
            "running": self.running,
            "error": self.error,
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

        def _origin_ok(ws) -> bool:
            """דפדפן שולח Origin; מקבלים רק את דף הרימוט שלנו (פורט 1780).
            בלי זה כל אתר שפתוח בדפדפן ברשת יכול לנסות PIN-ים ברקע."""
            try:
                origin = (getattr(ws, "request", None).headers.get("Origin")
                          if getattr(ws, "request", None) else None)
            except Exception:
                origin = None
            if not origin:
                return True  # לקוח שאינו דפדפן (או ספרייה ישנה) — אין Origin
            return origin.endswith(f":{self.HTTP_PORT}")

        async def handler(ws):
            authed = False
            bad_pins = 0
            if not _origin_ok(ws):
                await ws.close()
                return
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
                            bad_pins += 1
                            await asyncio.sleep(0.6 * bad_pins)   # מאט ניסיונות ניחוש
                            await ws.send(json.dumps({"type": "badpin"}))
                            if bad_pins >= 5:
                                await ws.close()                  # 10,000 צירופים? לא מהחיבור הזה
                                return
                    elif authed:
                        try:
                            t = msg.get("type")
                            if t == "set":
                                self.api.set_level(msg.get("level", 0))
                            elif t == "fade":
                                self.api.fade_to(msg.get("target", 0), msg.get("seconds", 5))
                            elif t == "stop":
                                self.api.stop_fade()
                            elif t == "fadeSeconds":
                                self.api.set_fade_seconds(msg.get("seconds", 5))
                        except Exception:
                            pass  # הודעה פגומה (level שאינו מספר וכו') לא תנתק את הטלפון
            except websockets.exceptions.ConnectionClosed:
                pass  # טלפון נעל מסך / יצא מהדף — צפוי, יתחבר מחדש לבד
            finally:
                self.clients.discard(ws)

        async def broadcaster():
            while True:
                try:
                    if self.clients:
                        payload = json.dumps({"type": "status", **self.api.status()})
                        await asyncio.gather(
                            *[c.send(payload) for c in list(self.clients)],
                            return_exceptions=True,
                        )
                except Exception:
                    pass  # status() נכשל רגעית (החלפת התקן פלט) — לא מפילים את השרת
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
        self.ride = AutoRide(self)
        self.license = License()

    # --- Auto-Ride ---
    def ride_toggle(self):
        if not self.license.allows("ride"):
            return self.status()
        self.ride.toggle()
        return self.status()

    # --- MIDI (שלט פיזי) ---
    def midi_info(self):
        return self.midi.info()

    def midi_learn(self, slot):
        if not self.license.allows("midi"):
            return self.midi.info()
        return self.midi.learn(slot)

    def midi_cancel_learn(self):
        return self.midi.cancel_learn()

    def midi_clear(self, slot):
        return self.midi.clear(slot)

    def remote_start(self):
        """מדליק את השליטה מהטלפון ומחזיר URL + PIN + QR."""
        if not self.license.allows("remote"):
            return {"running": False, "error": "Phone remote is a Pro feature"}
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
            "ride": self.ride.info(),
            "pro": self.license.is_pro(),
        }

    def set_fade_seconds(self, seconds):
        """בחירת זמן פייד מכל מסך — מתעדכנת בכולם דרך הסטטוס."""
        s = float(seconds)
        if 0.5 <= s <= 60:
            self.fade_seconds = s
        return self.status()

    def set_level(self, level):
        """גרירה ידנית של הפיידר — מבטלת פייד פעיל. ברכיבה: נועל יעד חדש."""
        self.engine.stop()
        self.vol.set(level)
        self.engine.remember_level(float(level))
        self.ride.notify_manual()
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


def _ride_test():
    """מצב שירות: `OneFader --ride-test` מדפיס את מד ה-Auto-Ride ל-15 שניות.
    כותב גם ל-log קבוע — כשמפעילים דרך open/דאבל-קליק אין stdout."""
    logpath = os.path.join(os.path.expanduser("~"), "onefader-ride-test.log")
    logf = open(logpath, "w", encoding="utf-8")

    def out(*a):
        print(*a, flush=True)
        print(*a, file=logf, flush=True)

    out("creating system-audio meter…")
    try:
        meter = make_meter()
        meter.start()
    except Exception as e:
        out("METER UNAVAILABLE:", e)
        return
    for i in range(30):
        time.sleep(0.5)
        out(f"[{i:02}] rms_db = {meter.rms_db}")
    meter.stop()
    out("done")


def main():
    if "--ride-test" in sys.argv:
        _ride_test()
        return
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
