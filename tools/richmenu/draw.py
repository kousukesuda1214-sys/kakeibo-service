from PIL import Image, ImageDraw, ImageFont
import math

W, H = 2500, 1686
S = 2  # 描画は2倍で行い、最後に縮小して線をなめらかにする
img = Image.new("RGB", (W * S, H * S), "#EEF3EF")
d = ImageDraw.Draw(img)
BOLD = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
MED = "/usr/share/fonts/opentype/noto/NotoSansCJK-Medium.ttc"
GREEN, DARK, WHITE, SUB = "#06C755", "#1F4D3A", "#FFFFFF", "#5E7A6C"

cols = [0, 833, 1667, 2500]; rows = [0, 843, 1686]
tiles = [
    ("今日の決算", "いつでも最新の数字", "chart", True),
    ("家計簿を開く", "スプレッドシート", "sheet", False),
    ("設定", "締め日・通知の時刻", "gear", False),
    ("今月のカテゴリ別", "何に使ったか", "pie", False),
    ("使い方", "できること・困ったとき", "help", False),
    ("連携・解除", "Googleアカウント", "link", False),
]

def icon(kind, cx, cy, r, fg, bg):
    lw = int(r * 0.13)
    if kind == "chart":
        bw = r * 0.34; base = cy + r * 0.62
        for i, h in enumerate([0.55, 0.95, 1.35]):
            x = cx - r * 0.72 + i * (bw + r * 0.2)
            d.rounded_rectangle([x, base - r * h, x + bw, base], radius=r * 0.08, fill=fg)
    elif kind == "sheet":
        d.rounded_rectangle([cx - r * .8, cy - r * .85, cx + r * .8, cy + r * .85], radius=r * .12, outline=fg, width=lw)
        d.rectangle([cx - r * .8, cy - r * .85, cx + r * .8, cy - r * .42], fill=fg)
        for f in (-0.05, 0.38):
            d.line([cx - r * .8, cy + r * f, cx + r * .8, cy + r * f], fill=fg, width=lw // 2 + 2)
        d.line([cx - r * .15, cy - r * .42, cx - r * .15, cy + r * .85], fill=fg, width=lw // 2 + 2)
    elif kind == "gear":
        for i in range(8):
            a = i * math.pi / 4
            x, y = cx + math.cos(a) * r * .78, cy + math.sin(a) * r * .78
            d.ellipse([x - r * .2, y - r * .2, x + r * .2, y + r * .2], fill=fg)
        d.ellipse([cx - r * .72, cy - r * .72, cx + r * .72, cy + r * .72], fill=fg)
        d.ellipse([cx - r * .3, cy - r * .3, cx + r * .3, cy + r * .3], fill=bg)
    elif kind == "pie":
        box = [cx - r * .85, cy - r * .85, cx + r * .85, cy + r * .85]
        d.pieslice(box, -90, 150, fill=fg)
        d.pieslice(box, 150, 270, fill=fg, outline=bg, width=0)
        d.pieslice([b + (8 if i < 2 else -8) * S for i, b in enumerate(box)], 150, 270, fill="#9BE3BA")
    elif kind == "help":
        d.ellipse([cx - r * .85, cy - r * .85, cx + r * .85, cy + r * .85], outline=fg, width=lw)
        f = ImageFont.truetype(BOLD, int(r * 1.2), index=0)
        d.text((cx, cy + r * .02), "?", font=f, fill=fg, anchor="mm")
    elif kind == "link":
        for dx, ang in ((-0.33, -40), (0.33, -40)):
            ox, oy = cx + r * dx, cy - r * dx * 0.2
            w, h = r * .55, r * 1.05
            ring = Image.new("RGBA", (int(w * 2 + 40), int(h * 2 + 40)), (0, 0, 0, 0))
            rd = ImageDraw.Draw(ring)
            rd.rounded_rectangle([20, 20, 20 + w * 2 - 1, 20 + h * 2 - 1], radius=w, outline=fg, width=lw)
            ring = ring.rotate(ang, expand=True, resample=Image.BICUBIC)
            img.paste(ring, (int(ox - ring.width / 2), int(oy - ring.height / 2)), ring)

pad = 22 * S
for i, (label, sub, kind, primary) in enumerate(tiles):
    c, rr = i % 3, i // 3
    x0, y0, x1, y1 = cols[c] * S + pad, rows[rr] * S + pad, cols[c + 1] * S - pad, rows[rr + 1] * S - pad
    bg = GREEN if primary else WHITE
    fg = WHITE if primary else GREEN
    d.rounded_rectangle([x0, y0, x1, y1], radius=48 * S, fill=bg)
    cx = (x0 + x1) / 2
    icon(kind, cx, y0 + (y1 - y0) * 0.36, 150 * S, fg, bg)
    f1 = ImageFont.truetype(BOLD, (104 if len(label) <= 6 else 92) * S, index=0)
    f2 = ImageFont.truetype(MED, 54 * S, index=0)
    d.text((cx, y0 + (y1 - y0) * 0.70), label, font=f1, fill=WHITE if primary else DARK, anchor="mm")
    d.text((cx, y0 + (y1 - y0) * 0.84), sub, font=f2, fill="#E3FBEC" if primary else SUB, anchor="mm")

img = img.resize((W, H), Image.LANCZOS)
img.save("richmenu.png", optimize=True)
import os; print("サイズ", img.size, "容量", os.path.getsize("richmenu.png") // 1024, "KB")
