"""把仓库地图渲染成 PNG（机器人定位图、编辑器导出共用）。"""
import io
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

from . import config, maps

TYPE_FILL = {
    maps.T_UNDEF: (248, 249, 250),
    maps.T_FUNC: (255, 236, 179),
    maps.T_STORAGE: (206, 224, 252),
    maps.T_CORRIDOR: (228, 231, 235),
}
BORDER = (73, 80, 87)
HILITE = (224, 49, 49)
TEXT = (33, 37, 41)
HEADER_H = 44


@lru_cache(maxsize=64)
def font(size: int):
    try:
        return ImageFont.truetype(config.FONT_PATH, max(8, int(size)))
    except OSError:
        return ImageFont.load_default()


def _hex(c: str):
    c = (c or "#888888").lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def _fit_text(draw, text, max_w, max_h, start):
    size = start
    while size > 8:
        f = font(size)
        l, t, r, b = draw.textbbox((0, 0), text, font=f)
        if r - l <= max_w and b - t <= max_h:
            return f, (r - l, b - t, l, t)
        size -= 1
    f = font(8)
    l, t, r, b = draw.textbbox((0, 0), text, font=f)
    return f, (r - l, b - t, l, t)


def _center_text(draw, box, text, color, start_size, stroke=0):
    x0, y0, x1, y1 = box
    f, (w, h, l, t) = _fit_text(draw, text, max(4, x1 - x0 - 4), max(4, y1 - y0 - 2), start_size)
    draw.text(((x0 + x1 - w) / 2 - l, (y0 + y1 - h) / 2 - t), text, font=f, fill=color,
              stroke_width=stroke, stroke_fill=(255, 255, 255))


def render(m: dict, highlights: dict | None = None, show_zones: bool = True, title: str = "",
           max_side: int = 2000, show_grid: bool = False) -> bytes:
    """highlights: {(区块字母, 货架编码): 拣货序号}"""
    highlights = highlights or {}
    a = maps.analyze(m)
    rows, cols = int(m["rows"]), int(m["cols"])
    cp = max(6.0, min(float(m.get("cell", 20)) * 1.5, max_side / max(cols, rows)))
    W, H = int(cols * cp), int(rows * cp)
    head_h = max(HEADER_H, int(W / 26))
    img = Image.new("RGB", (W, H + head_h), (255, 255, 255))
    d = ImageDraw.Draw(img)
    oy = head_h
    X = lambda c: int(round(c * cp))
    Y = lambda r: int(round(r * cp)) + oy

    types, bids, zones = m["types"], m["bids"], m["zones"]
    for r in range(rows):
        tr = types[r]
        for c in range(cols):
            d.rectangle([X(c), Y(r), X(c + 1) - 1, Y(r + 1) - 1], fill=TYPE_FILL.get(int(tr[c]), TYPE_FILL[0]))
    if show_grid and cp >= 8:
        for c in range(cols + 1):
            d.line([X(c), oy, X(c), oy + H], fill=(233, 236, 239))
        for r in range(rows + 1):
            d.line([0, Y(r), W, Y(r)], fill=(233, 236, 239))

    # 仓库区块（字母）半透明覆盖
    if show_zones and a["zones"]:
        ov = Image.new("RGBA", img.size, (0, 0, 0, 0))
        od = ImageDraw.Draw(ov)
        for r in range(rows):
            zr = zones[r]
            for c in range(cols):
                z = zr[c]
                if z != ".":
                    od.rectangle([X(c), Y(r), X(c + 1) - 1, Y(r + 1) - 1], fill=_hex(a["zones"][z]["color"]) + (52,))
        img = Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB")
        d = ImageDraw.Draw(img)
        for r in range(rows):
            for c in range(cols):
                z = zones[r][c]
                if c + 1 < cols and zones[r][c + 1] != z:
                    for zz in {z, zones[r][c + 1]} - {"."}:
                        d.line([X(c + 1), Y(r), X(c + 1), Y(r + 1)], fill=_hex(a["zones"][zz]["color"]), width=2)
                if r + 1 < rows and zones[r + 1][c] != z:
                    for zz in {z, zones[r + 1][c]} - {"."}:
                        d.line([X(c), Y(r + 1), X(c + 1), Y(r + 1)], fill=_hex(a["zones"][zz]["color"]), width=2)

    # 区块边界
    for r in range(rows):
        br = bids[r]
        for c in range(cols):
            b = br[c]
            if c + 1 < cols and br[c + 1] != b and (b or br[c + 1]):
                d.line([X(c + 1), Y(r), X(c + 1), Y(r + 1)], fill=BORDER, width=2)
            if r + 1 < rows and bids[r + 1][c] != b and (b or bids[r + 1][c]):
                d.line([X(c), Y(r + 1), X(c + 1), Y(r + 1)], fill=BORDER, width=2)
    for c in range(cols):
        if bids[0][c]:
            d.line([X(c), Y(0), X(c + 1), Y(0)], fill=BORDER, width=2)
        if bids[rows - 1][c]:
            d.line([X(c), Y(rows) - 1, X(c + 1), Y(rows) - 1], fill=BORDER, width=2)
    for r in range(rows):
        if bids[r][0]:
            d.line([X(0), Y(r), X(0), Y(r + 1)], fill=BORDER, width=2)
        if bids[r][cols - 1]:
            d.line([X(cols) - 1, Y(r), X(cols) - 1, Y(r + 1)], fill=BORDER, width=2)

    # 高亮目标货架
    hl_blocks = []
    if highlights:
        ov = Image.new("RGBA", img.size, (0, 0, 0, 0))
        od = ImageDraw.Draw(ov)
        for b in a["blocks"].values():
            if b["type"] == "storage" and (b["zone"], b["shelf"]) in highlights:
                for (r, c) in b["cells"]:
                    od.rectangle([X(c), Y(r), X(c + 1) - 1, Y(r + 1) - 1], fill=HILITE + (95,))
                hl_blocks.append(b)
        img = Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB")
        d = ImageDraw.Draw(img)
        for b in hl_blocks:
            cells = set(b["cells"])
            for (r, c) in cells:
                if (r, c + 1) not in cells:
                    d.line([X(c + 1), Y(r), X(c + 1), Y(r + 1)], fill=HILITE, width=3)
                if (r, c - 1) not in cells:
                    d.line([X(c), Y(r), X(c), Y(r + 1)], fill=HILITE, width=3)
                if (r + 1, c) not in cells:
                    d.line([X(c), Y(r + 1), X(c + 1), Y(r + 1)], fill=HILITE, width=3)
                if (r - 1, c) not in cells:
                    d.line([X(c), Y(r), X(c + 1), Y(r)], fill=HILITE, width=3)

    # 文字：功能标注、货架编码、区块字母
    for b in a["blocks"].values():
        r0, c0, r1, c1 = b["bbox"]
        box = (X(c0), Y(r0), X(c1 + 1), Y(r1 + 1))
        start = int(min(cp * 3, (box[3] - box[1]) * 0.75))
        if b["type"] == "function" and b.get("label"):
            _center_text(d, box, b["label"], TEXT, start)
        elif b["type"] == "storage":
            label = f"{b['zone'] or '?'}-{b.get('shelf') or '??'}"
            _center_text(d, box, label, TEXT, start)
    if show_zones:
        for z, zi in a["zones"].items():
            rr, cc = zi["center"]
            s = int(max(cp * 2, min(cp * 5, (zi["cells"] ** 0.5) * cp * 0.35)))
            cx, cy = X(cc + 0.5), Y(rr + 0.5)
            _center_text(d, (cx - s, cy - s, cx + s, cy + s), z, _hex(zi["color"]), s, stroke=2)

    # 红点：每个目标货架在上边缘中间放一个红色圆点（带光圈），点里是拣货顺序
    if hl_blocks:
        rad = max(14, int(cp * 0.8), int(W / 60))
        pins = []
        for b in hl_blocks:
            r0, c0, _, c1 = b["bbox"]
            cx = (X(c0) + X(c1 + 1)) // 2
            cy = min(max(Y(r0), oy + rad + 4), oy + H - rad - 4)
            pins.append((cx, cy, highlights[(b["zone"], b["shelf"])]))
        halo = Image.new("RGBA", img.size, (0, 0, 0, 0))
        hd = ImageDraw.Draw(halo)
        for cx, cy, _ in pins:
            hr = int(rad * 1.9)
            hd.ellipse([cx - hr, cy - hr, cx + hr, cy + hr], fill=HILITE + (60,))
        img = Image.alpha_composite(img.convert("RGBA"), halo).convert("RGB")
        d = ImageDraw.Draw(img)
        for cx, cy, seq in pins:
            d.ellipse([cx - rad, cy - rad, cx + rad, cy + rad], fill=HILITE, outline=(255, 255, 255), width=3)
            _center_text(d, (cx - rad, cy - rad, cx + rad, cy + rad), str(seq), (255, 255, 255), int(rad * 1.25))

    # 标题栏
    d.rectangle([0, 0, W, head_h - 1], fill=(241, 243, 245))
    head = title or f"仓库 {m.get('wh_no', '')} {m.get('name', '')}".strip()
    hs = int(head_h * 0.46)
    d.text((head_h * 0.3, (head_h - hs * 1.15) / 2), head, font=font(hs), fill=TEXT)
    if highlights:
        tip = f"红点 = 要去的货架（共 {len(highlights)} 个，数字是拣货顺序）"
        ts = int(head_h * 0.34)
        tw = d.textlength(tip, font=font(ts))
        if tw + head_h + d.textlength(head, font=font(hs)) < W:
            d.text((W - tw - head_h * 0.3, (head_h - ts * 1.15) / 2), tip, font=font(ts), fill=HILITE)

    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()
