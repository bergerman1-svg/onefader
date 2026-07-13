# -*- coding: utf-8 -*-
"""מייצר את אייקון OneFader (מתוך עיצוב ה-app badge ב-logo.html)
ל-PNG 1024, ולאחר מכן ל-.icns (מק) ול-.ico (ווינדוס).
ציור ישיר ב-Pillow — בלי תלות חיצונית ברסטור SVG."""

import os
import subprocess
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
S = 4  # supersampling
W = 1024 * S

# --- צבעי המותג (מתוך logo.html) ---
BG_TOP = (27, 32, 41)      # #1B2029
BG_BOT = (14, 17, 22)      # #0E1116
LED = (255, 180, 84)       # #FFB454
LED_HI = (255, 208, 138)   # #FFD08A
TRACK = (11, 13, 17)       # #0B0D11
TRACK_LINE = (38, 44, 56)  # #262C38
CAP_TOP = (69, 78, 95)     # #454E5F
CAP_BOT = (28, 32, 41)     # #1C2029
CAP_LINE = (90, 99, 117)   # #5A6375


def vgrad(w, h, top, bot):
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        t = y / max(1, h - 1)
        px[0, y] = tuple(round(top[i] + (bot[i] - top[i]) * t) for i in range(3))
    return img.resize((w, h))  # column already filled per row


def vgrad_fast(w, h, top, bot):
    base = Image.new("RGB", (1, h))
    px = base.load()
    for y in range(h):
        t = y / max(1, h - 1)
        px[0, y] = tuple(round(top[i] + (bot[i] - top[i]) * t) for i in range(3))
    return base.resize((w, h))


def rounded_mask(w, h, r):
    m = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(m)
    d.rounded_rectangle([0, 0, w - 1, h - 1], radius=r, fill=255)
    return m


img = Image.new("RGBA", (W, W), (0, 0, 0, 0))

# רקע הבאדג' — ריבוע מעוגל עם גרדיאנט אנכי
badge = vgrad_fast(W, W, BG_TOP, BG_BOT).convert("RGBA")
badge.putalpha(rounded_mask(W, W, int(0.235 * W)))

# זוהר כתום עדין מלמעלה
glow = Image.new("RGBA", (W, W), (0, 0, 0, 0))
gd = ImageDraw.Draw(glow)
cx, gy = W // 2, int(0.28 * W)
for rad, a in [(int(0.42 * W), 60), (int(0.30 * W), 70), (int(0.18 * W), 80)]:
    gd.ellipse([cx - rad, gy - rad, cx + rad, gy + rad], fill=(*LED, a))
from PIL import ImageFilter
glow = glow.filter(ImageFilter.GaussianBlur(int(0.06 * W)))
badge = Image.alpha_composite(badge, glow)

d = ImageDraw.Draw(badge)

# --- הפיידר במרכז ---
# track אנכי
tw = int(0.085 * W)
tx0 = cx - tw // 2
ty0, ty1 = int(0.20 * W), int(0.80 * W)
d.rounded_rectangle([tx0, ty0, tx0 + tw, ty1], radius=tw // 2, fill=TRACK,
                    outline=TRACK_LINE, width=max(2, S))

# glow כתום בתחתית ה-track
gw = int(0.045 * W)
gx0 = cx - gw // 2
gseg = Image.new("RGBA", (W, W), (0, 0, 0, 0))
gsd = ImageDraw.Draw(gseg)
gtop = int(0.55 * W)
gsd.rounded_rectangle([gx0, gtop, gx0 + gw, ty1 - int(0.02 * W)], radius=gw // 2,
                      fill=(*LED, 255))
gseg = gseg.filter(ImageFilter.GaussianBlur(int(0.012 * W)))
badge = Image.alpha_composite(badge, gseg)
d = ImageDraw.Draw(badge)
# core בהיר של ה-glow
gsd2 = ImageDraw.Draw(badge)
gsd2.rounded_rectangle([gx0, gtop, gx0 + gw, ty1 - int(0.02 * W)], radius=gw // 2, fill=LED)

# cap
cap_w = int(0.44 * W)
cap_h = int(0.135 * W)
cxa = cx - cap_w // 2
cya = int(0.46 * W)
cap_grad = vgrad_fast(cap_w, cap_h, CAP_TOP, CAP_BOT).convert("RGBA")
cap_grad.putalpha(rounded_mask(cap_w, cap_h, int(0.28 * cap_h)))
badge.alpha_composite(cap_grad, (cxa, cya))
d = ImageDraw.Draw(badge)
d.rounded_rectangle([cxa, cya, cxa + cap_w, cya + cap_h], radius=int(0.28 * cap_h),
                    outline=CAP_LINE, width=max(2, S))
# קו LED כתום לרוחב ה-cap
lly = cya + cap_h // 2
lpad = int(0.10 * cap_w)
d.rounded_rectangle([cxa + lpad, lly - 6 * S, cxa + cap_w - lpad, lly + 6 * S],
                    radius=6 * S, fill=LED)

out = Image.alpha_composite(Image.new("RGBA", (W, W), (0, 0, 0, 0)), badge)
out = out.resize((1024, 1024), Image.LANCZOS)
png = os.path.join(HERE, "icon_1024.png")
out.save(png)
print("saved", png)

# --- .ico לווינדוס (רב-רזולוציה) ---
ico = os.path.join(HERE, "OneFader.ico")
out.save(ico, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("saved", ico)

# --- .icns למק דרך iconutil ---
iconset = os.path.join(HERE, "OneFader.iconset")
os.makedirs(iconset, exist_ok=True)
specs = [(16, "16x16"), (32, "16x16@2x"), (32, "32x32"), (64, "32x32@2x"),
         (128, "128x128"), (256, "128x128@2x"), (256, "256x256"),
         (512, "256x256@2x"), (512, "512x512"), (1024, "512x512@2x")]
for size, name in specs:
    out.resize((size, size), Image.LANCZOS).save(
        os.path.join(iconset, f"icon_{name}.png"))
subprocess.run(["iconutil", "-c", "icns", iconset,
                "-o", os.path.join(HERE, "OneFader.icns")], check=True)
print("saved", os.path.join(HERE, "OneFader.icns"))
