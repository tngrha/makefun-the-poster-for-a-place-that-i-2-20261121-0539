#!/usr/bin/env python3
"""SOLMARA - a travel poster for a place that isn't there.

A harbor town built along the rim of a colossal sleeping sea-turtle's shell,
at dusk: lit windows, a lantern-strung market quay, a wooden dock, fishing
boats with warm lamps on a calm amber sea.

Pure Python 3.13 standard library only: a hand-rolled RGB framebuffer, an
original 8x10 bitmap font, flat geometric shapes and a hand PNG encoder.
Writes /output/entry.png (PNG, 1024x1024, <= 1 MiB). Fully deterministic:
no randomness, no clock, no network; two runs are byte-identical.
"""

import binascii
import math
import os
import struct
import sys
import zlib

W = 1024
H = 1024

# --------------------------------------------------------------------------
# Framebuffer and drawing primitives
# --------------------------------------------------------------------------
buf = bytearray(W * H * 3)


def px(x, y, c):
    if 0 <= x < W and 0 <= y < H:
        i = (y * W + x) * 3
        buf[i] = c[0]
        buf[i + 1] = c[1]
        buf[i + 2] = c[2]


def blend_px(x, y, c, a):
    if 0 <= x < W and 0 <= y < H:
        i = (y * W + x) * 3
        buf[i] = int(buf[i] * (1 - a) + c[0] * a)
        buf[i + 1] = int(buf[i + 1] * (1 - a) + c[1] * a)
        buf[i + 2] = int(buf[i + 2] * (1 - a) + c[2] * a)


def hline(x0, x1, y, c):
    if y < 0 or y >= H:
        return
    if x0 > x1:
        x0, x1 = x1, x0
    x0 = max(0, x0)
    x1 = min(W - 1, x1)
    if x1 < x0:
        return
    i = (y * W + x0) * 3
    n = x1 - x0 + 1
    buf[i:i + 3 * n] = bytes(c) * n


def rect(x, y, w, h, c):
    for yy in range(y, y + h):
        hline(x, x + w - 1, yy, c)


def blend_rect(x, y, w, h, c, a):
    for yy in range(y, y + h):
        for xx in range(x, x + w):
            blend_px(xx, yy, c, a)


def ellipse(cx, cy, rx, ry, c):
    for dy in range(-ry, ry + 1):
        t = 1.0 - (dy * dy) / float(ry * ry)
        if t < 0:
            continue
        dx = int(rx * math.sqrt(t))
        hline(cx - dx, cx + dx, cy + dy, c)


def blend_ellipse(cx, cy, rx, ry, c, a):
    for dy in range(-ry, ry + 1):
        t = 1.0 - (dy * dy) / float(ry * ry)
        if t < 0:
            continue
        dx = int(rx * math.sqrt(t))
        for xx in range(cx - dx, cx + dx + 1):
            blend_px(xx, cy + dy, c, a)


def circle(cx, cy, r, c):
    ellipse(cx, cy, r, r, c)


def glow(cx, cy, r, c):
    # soft radial glow: concentric blended ellipses, strongest at centre
    steps = 6
    for s in range(steps, 0, -1):
        rr = max(1, int(r * s / steps))
        a = 0.10 + 0.10 * (steps - s)
        blend_ellipse(cx, cy, rr, rr, c, a)


def poly(points, c):
    ys = [p[1] for p in points]
    y0, y1 = min(ys), max(ys)
    n = len(points)
    for y in range(y0, y1 + 1):
        xs = []
        for i in range(n):
            x1, yy1 = points[i]
            x2, yy2 = points[(i + 1) % n]
            if yy1 == yy2:
                continue
            if min(yy1, yy2) <= y < max(yy1, yy2):
                t = (y - yy1) / float(yy2 - yy1)
                xs.append(x1 + t * (x2 - x1))
        xs.sort()
        for i in range(0, len(xs) - 1, 2):
            hline(int(xs[i]), int(xs[i + 1]), y, c)


def blend_poly(points, c, a):
    ys = [p[1] for p in points]
    y0, y1 = min(ys), max(ys)
    n = len(points)
    for y in range(y0, y1 + 1):
        xs = []
        for i in range(n):
            x1, yy1 = points[i]
            x2, yy2 = points[(i + 1) % n]
            if yy1 == yy2:
                continue
            if min(yy1, yy2) <= y < max(yy1, yy2):
                t = (y - yy1) / float(yy2 - yy1)
                xs.append(x1 + t * (x2 - x1))
        xs.sort()
        for i in range(0, len(xs) - 1, 2):
            for xx in range(int(xs[i]), int(xs[i + 1]) + 1):
                blend_px(xx, y, c, a)


def line(x0, y0, x1, y1, c, wdt=1):
    dx = abs(x1 - x0)
    dy = -abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx + dy
    x, y = x0, y0
    while True:
        for oy in range(wdt):
            for ox in range(wdt):
                px(x + ox, y + oy, c)
        if x == x1 and y == y1:
            break
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x += sx
        if e2 <= dx:
            err += dx
            y += sy


def lerp(c0, c1, t):
    return (int(c0[0] + (c1[0] - c0[0]) * t),
            int(c0[1] + (c1[1] - c0[1]) * t),
            int(c0[2] + (c1[2] - c0[2]) * t))


def vgrad(x, y, w, h, stops):
    # stops: list of (offset 0..1, colour), top to bottom
    for row in range(h):
        t = row / float(max(1, h - 1))
        c = stops[-1][1]
        for i in range(len(stops) - 1):
            o0, c0 = stops[i]
            o1, c1 = stops[i + 1]
            if o0 <= t <= o1:
                c = lerp(c0, c1, (t - o0) / float(o1 - o0))
                break
        hline(x, x + w - 1, y + row, c)


# deterministic pseudo-random (fixed seed)
_rng = [20261121]


def rnd():
    _rng[0] = (_rng[0] * 1103515245 + 12345) & 0x7FFFFFFF
    return _rng[0]


def rnd_range(a, b):
    return a + rnd() % (b - a + 1)


# --------------------------------------------------------------------------
# Original 8x10 bitmap font (only the glyphs this poster sets, plus a few)
# --------------------------------------------------------------------------
FONT = {
    "S": [".######.", "##....##", "##......", "##......", ".#####..",
          "......##", "......##", "......##", "##....##", ".######."],
    "O": [".######.", "##....##", "##....##", "##....##", "##....##",
          "##....##", "##....##", "##....##", "##....##", ".######."],
    "L": ["##......", "##......", "##......", "##......", "##......",
          "##......", "##......", "##......", "##....##", "#######."],
    "M": ["##....##", "###..###", "##.##.##", "##.##.##", "##....##",
          "##....##", "##....##", "##....##", "##....##", "##....##"],
    "A": ["...##...", "..####..", "..#..#..", ".##..##.", ".##..##.",
          "########", "##....##", "##....##", "##....##", "##....##"],
    "R": ["######..", "##...##.", "##....##", "##....##", "######..",
          "##.##...", "##..##..", "##...##.", "##....##", "##....##"],
    " ": ["........"] * 10,
}


def text(s, x, y, scale, c, tracking=14):
    cx = x
    for ch in s:
        g = FONT[ch]
        for r, row in enumerate(g):
            for col, bit in enumerate(row):
                if bit == "#":
                    rect(cx + col * scale, y + r * scale, scale, scale, c)
        cx += 8 * scale + tracking
    return cx - tracking


def text_width(s, scale, tracking=14):
    return len(s) * 8 * scale + (len(s) - 1) * tracking


# --------------------------------------------------------------------------
# Palette
# --------------------------------------------------------------------------
CREAM = (246, 232, 200)
CREAM_DIM = (232, 213, 168)
INK_TEAL = (10, 46, 61)
SKY_TOP = (13, 52, 71)
SKY_MID = (31, 106, 114)
SKY_LOW = (238, 143, 58)
SUN_CORE = (255, 190, 84)
SUN_EDGE = (242, 118, 46)
SEA_TOP = (242, 153, 74)
SEA_MID = (184, 92, 46)
SEA_LOW = (94, 52, 40)
SHELL = (23, 80, 63)
SHELL_RIM = (42, 122, 92)
SHELL_DARK = (16, 60, 47)
HEAD_GREEN = (35, 102, 78)
HOUSE_DARK = (58, 36, 48)
HOUSE_CREAM = (232, 213, 168)
ROOF = (181, 80, 46)
ROOF_DARK = (140, 58, 38)
WINDOW = (255, 200, 77)
LAMP = (255, 214, 110)
WOOD = (96, 62, 40)
WOOD_DARK = (66, 42, 28)
SILHOUETTE = (24, 30, 38)

HORIZON = 640
SHELL_CX, SHELL_CY, SHELL_RX, SHELL_RY = 512, 672, 405, 215


def shell_top_y(x):
    t = 1.0 - ((x - SHELL_CX) / float(SHELL_RX)) ** 2
    if t < 0:
        return SHELL_CY
    return int(SHELL_CY - SHELL_RY * math.sqrt(t))


# --------------------------------------------------------------------------
# Scene
# --------------------------------------------------------------------------
def draw_sky():
    vgrad(0, 0, W, HORIZON + 2,
          [(0.0, SKY_TOP), (0.45, SKY_MID), (0.78, (120, 120, 96)),
           (1.0, SKY_LOW)])
    # sparse early stars in the upper sky
    for _ in range(90):
        x = rnd_range(30, W - 30)
        y = rnd_range(30, 340)
        b = rnd_range(0, 2)
        c = lerp(SKY_TOP, CREAM, 0.45 + 0.2 * b)
        px(x, y, c)
        if b == 2:
            px(x + 1, y, c)


def draw_sun():
    cx, cy = 512, 600
    circle(cx, cy, 272, SUN_EDGE)
    circle(cx, cy, 218, (249, 164, 62))
    circle(cx, cy, 168, SUN_CORE)


def draw_sea():
    vgrad(0, HORIZON, W, H - HORIZON,
          [(0.0, SEA_TOP), (0.35, SEA_MID), (1.0, SEA_LOW)])


def draw_sea_streaks():
    # calm horizontal streaks
    for i in range(46):
        y = HORIZON + 14 + i * 8 + rnd_range(0, 4)
        if y >= H - 30:
            break
        x = rnd_range(40, W - 240)
        ln = rnd_range(40, 200)
        c = lerp(SEA_TOP, CREAM, 0.25) if i % 2 == 0 else SEA_MID
        blend_rect(x, y, ln, 2, c, 0.35)


def draw_turtle():
    # shell: lighter rim ellipse peeking above the main dome
    ellipse(SHELL_CX, SHELL_CY - 12, SHELL_RX + 4, SHELL_RY + 4, SHELL_RIM)
    ellipse(SHELL_CX, SHELL_CY, SHELL_RX, SHELL_RY, SHELL)
    # shell plate arcs (subtle, darker)
    for k in range(5):
        rx = SHELL_RX - 40 - k * 62
        ry = SHELL_RY - 22 - k * 30
        if rx <= 20 or ry <= 10:
            break
        for ang in range(195, 346, 2):
            a = math.radians(ang)
            x = int(SHELL_CX + rx * math.cos(a))
            y = int(SHELL_CY + ry * math.sin(a))
            if y < HORIZON + 30:
                px(x, y, SHELL_DARK)
                px(x, y + 1, SHELL_DARK)
    # neck + head resting at the waterline on the right, eye closed (sleeping)
    poly([(828, 622), (884, 560), (922, 572), (872, 638)], HEAD_GREEN)
    ellipse(902, 578, 54, 40, HEAD_GREEN)
    ellipse(922, 570, 34, 27, HEAD_GREEN)
    # closed eye: a gentle downward arc
    for dx in range(-13, 14):
        yy = 566 + int(6 * (1 - (dx / 13.0) ** 2))
        px(920 + dx, yy, SHELL_DARK)
        px(920 + dx, yy + 1, SHELL_DARK)
        px(920 + dx, yy + 2, SHELL_DARK)
    # nostril dot + calm mouth line
    circle(946, 584, 2, SHELL_DARK)
    line(916, 596, 946, 590, SHELL_DARK, 2)


def draw_flipper():
    # front flipper paddling at the shell's waterline, tucked against it
    ellipse(845, 660, 82, 26, SHELL_DARK)
    ellipse(845, 652, 76, 22, SHELL)
    blend_ellipse(845, 678, 86, 10, SEA_LOW, 0.30)


def window_glow(x, y, w=5, h=6):
    blend_rect(x - 3, y - 3, w + 6, h + 6, WINDOW, 0.30)
    rect(x, y, w, h, WINDOW)


def draw_house(x, base_y, w, h, wall, roof_c, nwin, seed_off):
    # body
    rect(x, base_y - h, w, h, wall)
    # roof: triangle with slight overhang
    poly([(x - 4, base_y - h), (x + w + 4, base_y - h),
          (x + w // 2, base_y - h - max(10, w // 2))], roof_c)
    # door
    rect(x + w // 2 - 3, base_y - 12, 6, 12, SILHOUETTE)
    # lit windows, deterministic per house
    spots = [(x + 5, base_y - h + 7), (x + w - 11, base_y - h + 7),
             (x + 5, base_y - 22), (x + w - 11, base_y - 22),
             (x + w // 2 - 3, base_y - h + 8)]
    for i in range(nwin):
        wx, wy = spots[(i + seed_off) % len(spots)]
        if wy + 6 < base_y - 12 or wx + 5 < x + w // 2 - 4 or wx > x + w // 2 + 3:
            window_glow(wx, wy)


def draw_lighthouse(x, base_y):
    top_y = base_y - 128
    # tapered tower: cream with two terracotta bands
    poly([(x - 13, base_y), (x + 13, base_y), (x + 8, top_y), (x - 8, top_y)],
         CREAM)
    for i, by in enumerate((base_y - 34, base_y - 78)):
        wdt = 13 - i * 2
        rect(x - wdt, by - 12, wdt * 2, 12, ROOF)
    # gallery + lamp room
    rect(x - 11, top_y - 4, 22, 4, SILHOUETTE)
    rect(x - 7, top_y - 18, 14, 14, SILHOUETTE)
    glow(x, top_y - 11, 22, LAMP)
    rect(x - 3, top_y - 14, 6, 7, LAMP)
    poly([(x - 9, top_y - 18), (x + 9, top_y - 18), (x, top_y - 30)], ROOF_DARK)
    # door + one window
    rect(x - 3, base_y - 14, 6, 14, SILHOUETTE)
    window_glow(x - 2, base_y - 58, 4, 5)
    return (x, top_y - 11)


def draw_beam(lamp_xy):
    lx, ly = lamp_xy
    blend_poly([(lx, ly), (980, ly - 150), (980, ly - 40)], LAMP, 0.10)
    blend_poly([(lx, ly), (980, ly - 118), (980, ly - 72)], LAMP, 0.12)


def figure(x, y, c=SILHOUETTE, h=13):
    circle(x, y - h + 3, 3, c)
    rect(x - 2, y - h + 5, 5, h - 5, CREAM_DIM)


def lantern_string(x0, y0, x1, y1, n=7):
    # sagging string with glowing lanterns
    pts = []
    for i in range(n + 1):
        t = i / float(n)
        x = int(x0 + (x1 - x0) * t)
        y = int(y0 + (y1 - y0) * t + 14 * 4 * t * (1 - t))
        pts.append((x, y))
    for i in range(n):
        line(pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1],
             SILHOUETTE, 1)
    for i in range(1, n):
        x, y = pts[i]
        glow(x, y + 4, 8, LAMP)
        circle(x, y + 4, 3, LAMP)


def awning_stall(x, base_y, stripe_a, stripe_b):
    # posts + striped awning + counter
    rect(x, base_y - 26, 3, 26, WOOD_DARK)
    rect(x + 37, base_y - 26, 3, 26, WOOD_DARK)
    for i in range(5):
        c = stripe_a if i % 2 == 0 else stripe_b
        poly([(x - 3 + i * 9, base_y - 26), (x + 6 + i * 9, base_y - 26),
              (x + 2 + i * 9, base_y - 38)], c)
    rect(x + 2, base_y - 12, 36, 12, WOOD)
    # goods: little warm dots on the counter
    for gx in range(x + 6, x + 34, 7):
        px(gx, base_y - 14, WINDOW)
        px(gx + 1, base_y - 14, WINDOW)


def draw_town():
    # houses along the shell rim, left to right; lighthouse left-of-centre
    houses = [
        # x, w, h, wall, roof, nwin
        (196, 38, 30, HOUSE_CREAM, ROOF, 2),
        (248, 34, 26, HOUSE_DARK, ROOF_DARK, 3),
        (300, 42, 34, HOUSE_CREAM, ROOF, 2),
        (428, 36, 28, HOUSE_DARK, ROOF, 3),
        (478, 44, 36, HOUSE_CREAM, ROOF_DARK, 3),
        (538, 34, 26, HOUSE_DARK, ROOF, 2),
        (588, 40, 32, HOUSE_CREAM, ROOF, 3),
        (644, 34, 27, HOUSE_DARK, ROOF_DARK, 2),
        (694, 38, 30, HOUSE_CREAM, ROOF, 2),
        (748, 32, 24, HOUSE_DARK, ROOF, 2),
    ]
    for idx, (x, w, h, wall, roof_c, nwin) in enumerate(houses):
        base = shell_top_y(x + w // 2) + 10
        draw_house(x, base, w, h, wall, roof_c, nwin, idx)
    lamp_xy = draw_lighthouse(392, shell_top_y(392) + 12)

    # chimney smoke from one house
    sx, sy = 500, shell_top_y(500) - 44
    for i in range(4):
        blend_ellipse(sx + i * 7, sy - i * 12, 5 + i * 3, 4 + i * 2,
                      CREAM, 0.22)

    # laundry line between two rooftops
    lx0, ly0 = 522, shell_top_y(522) - 30
    lx1, ly1 = 588, shell_top_y(588) - 24
    line(lx0, ly0, lx1, ly1, SILHOUETTE, 1)
    cloth = [CREAM, ROOF, (64, 140, 150), CREAM_DIM]
    for i, cc in enumerate(cloth):
        t = 0.18 + i * 0.2
        cx = int(lx0 + (lx1 - lx0) * t)
        cy = int(ly0 + (ly1 - ly0) * t)
        rect(cx, cy + 1, 9, 11, cc)

    # market quay on the shell's front slope: stalls, lanterns, figures
    quay = [(288, ROOF, CREAM), (408, (64, 140, 150), CREAM),
            (528, ROOF, CREAM), (648, (64, 140, 150), CREAM)]
    posts = []
    for x, sa, sb in quay:
        base = shell_top_y(x + 20) + 64
        awning_stall(x, base, sa, sb)
        posts.append((x - 6, base - 34))
    posts.append((712, shell_top_y(712) + 30))
    for i in range(len(posts) - 1):
        lantern_string(posts[i][0], posts[i][1], posts[i + 1][0],
                       posts[i + 1][1])
    # tiny figures strolling the quay
    for fx in (330, 470, 566, 690):
        figure(fx, shell_top_y(fx) + 64)
    # window-light reflections at the waterline
    for x in range(230, 800, 18):
        if rnd() % 3:
            blend_rect(x, HORIZON + 8 + rnd_range(0, 10), 3,
                       rnd_range(10, 26), WINDOW, 0.35)
    return lamp_xy


def draw_dock_and_boats():
    # wooden dock from the shell's front-left into the sea
    poly([(268, 636), (140, 716), (128, 706), (252, 628)], WOOD)
    for t in (0.15, 0.4, 0.65, 0.9):
        x = int(260 + (136 - 260) * t)
        y = int(632 + (712 - 632) * t)
        rect(x - 3, y, 6, 26, WOOD_DARK)
        line(x - 14, y + 4, x + 14, y - 2, WOOD_DARK, 2)
    # lantern at the dock end + a fisher
    glow(138, 700, 16, LAMP)
    rect(136, 690, 4, 16, WOOD_DARK)
    circle(138, 688, 4, LAMP)
    figure(150, 714)
    line(152, 706, 176, 724, SILHOUETTE, 1)
    line(176, 724, 176, 744, CREAM_DIM, 1)
    blend_rect(136, 728, 4, 30, LAMP, 0.4)

    # fishing boats with lamps and reflections
    boats = [(392, 770, 1.0), (640, 716, 0.72), (828, 792, 0.85),
             (246, 842, 0.62)]
    for bx, by, s in boats:
        hw = int(46 * s)
        hh = int(16 * s)
        poly([(bx - hw, by), (bx + hw, by), (bx + hw - 12, by + hh),
              (bx - hw + 12, by + hh)], SILHOUETTE)
        rect(bx - hw + 6, by - 3, hw * 2 - 12, 3, WOOD_DARK)
        mast_h = int(44 * s)
        line(bx, by, bx, by - mast_h, SILHOUETTE, 2)
        # small sail furled: a slim triangle
        poly([(bx + 2, by - mast_h + 4), (bx + 2, by - 4),
              (bx + int(20 * s), by - 4)], CREAM_DIM)
        glow(bx, by - mast_h - 4, int(16 * s) + 6, LAMP)
        circle(bx, by - mast_h - 4, 3, LAMP)
        # reflection: shimmering vertical dashes
        ry = by + hh + 6
        for i in range(7):
            wob = ((i * 7 + bx) % 5) - 2
            blend_rect(bx + wob - 2, ry + i * 9, 4, 5, LAMP,
                       0.42 - i * 0.05)


def draw_name():
    scale = 13
    tracking = 18
    s = "SOLMARA"
    tw = text_width(s, scale, tracking)
    x = (W - tw) // 2
    y = 84
    text(s, x + 5, y + 5, scale, INK_TEAL, tracking)
    text(s, x, y, scale, CREAM, tracking)
    # slim rule with a centre diamond under the name
    ry = y + 10 * scale + 26
    hline(x + 6, W // 2 - 26, ry, CREAM)
    hline(W // 2 + 26, x + tw - 6, ry, CREAM)
    poly([(W // 2, ry - 8), (W // 2 + 10, ry), (W // 2, ry + 8),
          (W // 2 - 10, ry)], CREAM)


def draw_frame():
    # vintage double-rule border
    for inset, wdt in ((14, 3), (26, 1)):
        rect(inset, inset, W - 2 * inset, wdt, CREAM)
        rect(inset, H - inset - wdt, W - 2 * inset, wdt, CREAM)
        rect(inset, inset, wdt, H - 2 * inset, CREAM)
        rect(W - inset - wdt, inset, wdt, H - 2 * inset, CREAM)


def render():
    draw_sky()
    draw_sun()
    draw_sea()
    draw_turtle()
    # repaint the sea below the waterline so the shell reads as a dome
    # emerging from the water, not a full oval floating in it
    vgrad(0, HORIZON + 2, W, H - HORIZON - 2,
          [(0.0, SEA_TOP), (0.35, SEA_MID), (1.0, SEA_LOW)])
    blend_ellipse(SHELL_CX, HORIZON + 10, SHELL_RX - 60, 14, SEA_LOW, 0.30)
    draw_sea_streaks()
    draw_flipper()
    lamp_xy = draw_town()
    draw_beam(lamp_xy)
    draw_dock_and_boats()
    draw_name()
    draw_frame()


# --------------------------------------------------------------------------
# PNG writer (hand-rolled, filter 0, zlib level 9)
# --------------------------------------------------------------------------
def _chunk(tag, data):
    return (struct.pack(">I", len(data)) + tag + data +
            struct.pack(">I", binascii.crc32(tag + data) & 0xFFFFFFFF))


def save_png(path):
    raw = bytearray()
    for y in range(H):
        raw.append(0)
        raw += buf[y * W * 3:(y + 1) * W * 3]
    ihdr = struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0)
    png = (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) +
           _chunk(b"IDAT", zlib.compress(bytes(raw), 9)) +
           _chunk(b"IEND", b""))
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "wb") as f:
        f.write(png)
    return len(png)


def main():
    render()
    out = sys.argv[1] if len(sys.argv) > 1 else "/output/entry.png"
    n = save_png(out)
    print("wrote %s (%d bytes)" % (out, n))


if __name__ == "__main__":
    main()
