#!/usr/bin/env python3
"""Generate the original `hero` sprite pack into assets/pets/hero/.

Everything here is drawn from scratch as parametric pixel art: a shared chibi
body rig posed per frame, plus a per-character palette and a few silhouette
switches (cape, mask style, build). Nothing is traced from or derived from any
existing character art, so the output is yours to ship.

Draws at 32x36 logical pixels and upscales 4x with nearest-neighbour, which is
what gives it the hard-edged pixel look the rest of the pack has.

    python tools/make_heroes.py && python tools/gen-metrics.py

Needs Pillow (pip install Pillow).
"""

import os
import sys

try:
    from PIL import Image, ImageChops, ImageDraw, ImageFilter
except ImportError:
    sys.exit("Pillow is required: pip install Pillow")

W, H = 32, 36          # logical canvas
SCALE = 4              # nearest-neighbour upscale
FLOOR = 35             # y of the soles
FPS = 8

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'assets', 'pets', 'hero')


# ---------------------------------------------------------------- characters

# suit / trim / skin / cape / emblem, plus silhouette switches.
# mask:  'helmet' full face with a visor slit
#        'cowl'   full head covering, eye slits
#        'domino' small mask, face shows
#        'none'   bare face
HEROES = [
    {
        'id': 'crimson', 'suit': (206, 52, 48), 'trim': (250, 196, 74),
        'skin': None, 'cape': None, 'emblem': (255, 236, 160),
        'mask': 'helmet', 'build': 'normal', 'eye': (126, 226, 255),
    },
    {
        'id': 'verdant', 'suit': (86, 170, 74), 'trim': (52, 62, 108),
        'skin': (86, 170, 74), 'cape': None, 'emblem': (52, 62, 108),
        'mask': 'none', 'build': 'bulky', 'eye': (28, 40, 30),
    },
    {
        'id': 'midnight', 'suit': (84, 102, 178), 'trim': (40, 46, 86),
        'skin': None, 'cape': (54, 64, 124), 'emblem': (190, 206, 255),
        'mask': 'cowl', 'build': 'normal', 'eye': (232, 240, 255),
    },
    {
        'id': 'frost', 'suit': (226, 238, 250), 'trim': (120, 190, 232),
        'skin': None, 'cape': (150, 206, 240), 'emblem': (90, 160, 214),
        'mask': 'helmet', 'build': 'slim', 'eye': (58, 140, 200),
    },
    {
        'id': 'ember', 'suit': (238, 138, 42), 'trim': (176, 62, 34),
        'skin': (247, 206, 166), 'cape': None, 'emblem': (255, 226, 130),
        'mask': 'domino', 'build': 'normal', 'eye': (44, 30, 24),
    },
    {
        'id': 'violet', 'suit': (126, 78, 190), 'trim': (222, 106, 190),
        'skin': (247, 206, 166), 'cape': (222, 106, 190), 'emblem': (245, 226, 255),
        'mask': 'domino', 'build': 'slim', 'eye': (44, 30, 24),
    },
]

BUILD = {
    'slim':   {'torso': 4, 'limb': 1, 'head': 5},
    'normal': {'torso': 5, 'limb': 2, 'head': 6},
    'bulky':  {'torso': 7, 'limb': 3, 'head': 6},
}


# ---------------------------------------------------------------- the rig

def rect(d, x0, y0, x1, y1, fill):
    """PIL rejects reversed rectangles; mirrored poses produce them constantly."""
    d.rectangle([min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)], fill=fill)


def shade(c, f):
    """Darken (f<1) or lighten (f>1) a colour, clamped."""
    return tuple(max(0, min(255, int(v * f))) for v in c)


def draw_hero(hero, pose):
    """One frame. `pose` carries the offsets that make it an animation."""
    img = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    b = BUILD[hero['build']]
    suit, trim = hero['suit'], hero['trim']
    skin = hero['skin'] or suit
    dark = shade(suit, 0.66)

    cx = W // 2 + pose.get('lean', 0)
    bob = pose.get('bob', 0)
    crouch = pose.get('crouch', 0)
    tuck = pose.get('tuck', 0)

    # vertical layout, pushed down by any crouch
    foot_y = FLOOR
    leg_top = 23 + crouch + bob
    torso_b = leg_top + 1
    torso_t = torso_b - (11 - crouch // 2)
    head_b = torso_t + 1
    head_t = head_b - (b['head'] * 2)

    half_t = b['torso']
    half_l = b['limb']

    # ---- cape, behind everything ----
    if hero['cape']:
        flare = pose.get('cape', 0)
        cape = hero['cape']
        d.polygon(
            [
                (cx - half_t - 1, torso_t + 1),
                (cx + half_t + 1, torso_t + 1),
                (cx + half_t + 3 + flare, foot_y - 2),
                (cx + flare // 2, foot_y - 4),
                (cx - half_t - 3 + flare, foot_y - 2),
            ],
            fill=cape,
        )
        d.polygon(
            [
                (cx - half_t - 1, torso_t + 1),
                (cx, torso_t + 3),
                (cx + half_t + 1, torso_t + 1),
            ],
            fill=shade(cape, 1.25),
        )

    # ---- legs ----
    if tuck:
        # knees up: short stubby legs angled forward
        for side, sx in ((-1, -1), (1, 1)):
            x0 = cx + sx * 1
            rect(d, x0 - half_l, leg_top, x0 + half_l + sx * 3, leg_top + 4, dark)
            rect(d, x0 + sx * 2, leg_top + 3, x0 + sx * 5, leg_top + 6, trim)
    else:
        swing = pose.get('legs', 0)
        for sx, off in ((-1, -swing), (1, swing)):
            x0 = cx + sx * (half_l + 1) + off
            rect(d, x0 - half_l, leg_top, x0 + half_l, foot_y - 3, dark)
            # boot
            rect(d, x0 - half_l - 1, foot_y - 3, x0 + half_l + 1, foot_y, trim)

    # ---- torso ----
    rect(d, cx - half_t, torso_t, cx + half_t, torso_b, suit)
    rect(d, cx - half_t, torso_t, cx - half_t + 1, torso_b, shade(suit, 1.2))
    rect(d, cx + half_t - 1, torso_t, cx + half_t, torso_b, shade(suit, 0.82))
    # belt
    rect(d, cx - half_t, torso_b - 2, cx + half_t, torso_b, trim)
    # emblem
    ey = torso_t + 3
    rect(d, cx - 1, ey, cx + 1, ey + 2, hero['emblem'])
    d.point((cx, ey - 1), fill=hero['emblem'])

    # ---- arms ----
    punch = pose.get('punch', 0)
    swing = pose.get('arms', 0)

    # back arm
    bx = cx - half_t - half_l - 1 - (0 if punch else 0)
    rect(d, bx - half_l, torso_t + 1 - swing, bx + half_l, torso_b - 2 - swing, shade(suit, 0.86))
    rect(d, bx - half_l, torso_b - 3 - swing, bx + half_l, torso_b - 1 - swing, trim)

    if punch:
        # front arm thrown straight out
        ay = torso_t + 3
        fx = cx + half_t
        rect(d, fx, ay, fx + 5 + punch, ay + half_l * 2, suit)
        rect(d, fx + 4 + punch, ay - 1, fx + 7 + punch, ay + half_l * 2 + 1, trim)
    elif pose.get('reach'):
        # both arms up
        for sx in (-1, 1):
            ax = cx + sx * (half_t + half_l + 1)
            rect(d, ax - half_l, torso_t - 5, ax + half_l, torso_t + 3, suit)
            rect(d, ax - half_l, torso_t - 7, ax + half_l, torso_t - 4, trim)
    else:
        fx = cx + half_t + half_l + 1
        rect(d, fx - half_l, torso_t + 1 + swing, fx + half_l, torso_b - 2 + swing, suit)
        rect(d, fx - half_l, torso_b - 3 + swing, fx + half_l, torso_b - 1 + swing, trim)

    # ---- head ----
    hw = b['head']
    rect(d, cx - hw, head_t, cx + hw, head_b, skin)

    mask = hero['mask']
    if mask == 'helmet':
        rect(d, cx - hw, head_t, cx + hw, head_b, suit)
        rect(d, cx - hw, head_t, cx + hw, head_t + 2, shade(suit, 1.18))
        rect(d, cx - hw + 1, head_t + 4, cx + hw - 1, head_t + 6, hero['eye'])
        rect(d, cx - hw, head_b - 2, cx + hw, head_b, trim)
    elif mask == 'cowl':
        rect(d, cx - hw, head_t, cx + hw, head_b, trim)
        rect(d, cx - hw + 1, head_t + 4, cx - 1, head_t + 6, hero['eye'])
        rect(d, cx + 1, head_t + 4, cx + hw - 1, head_t + 6, hero['eye'])
    elif mask == 'domino':
        d.rectangle([cx - hw, head_t, cx + hw, head_t + 3], fill=shade(skin, 0.72))   # hair
        d.rectangle([cx - hw, head_t + 3, cx + hw, head_t + 6], fill=trim)            # band
        rect(d, cx - hw + 1, head_t + 4, cx - 2, head_t + 5, (255, 255, 255))
        rect(d, cx + 2, head_t + 4, cx + hw - 1, head_t + 5, (255, 255, 255))
    else:
        d.rectangle([cx - hw, head_t, cx + hw, head_t + 2], fill=shade(skin, 0.6))    # hair
        rect(d, cx - hw + 2, head_t + 5, cx - hw + 3, head_t + 6, hero['eye'])
        rect(d, cx + hw - 3, head_t + 5, cx + hw - 2, head_t + 6, hero['eye'])
        d.rectangle([cx - 2, head_t + 8, cx + 2, head_t + 9], fill=shade(skin, 0.72))  # mouth

    return outline(img)


OUTLINE = (26, 24, 40, 255)


def outline(img):
    """Wrap the silhouette in a 1px dark border.

    Every other species in the pack is outlined, and without it these read as
    flat blocks of colour and disappear against a busy wallpaper.
    """
    alpha = img.getchannel('A').point(lambda v: 255 if v > 127 else 0)
    grown = alpha.filter(ImageFilter.MaxFilter(3))
    ring = ImageChops.subtract(grown, alpha)

    out = Image.new('RGBA', img.size, OUTLINE)
    out.putalpha(ring)
    out.alpha_composite(img)
    return out


# ---------------------------------------------------------------- animations

def frames_for(hero, action):
    """Pose list per action. Values are small integer offsets into the rig."""
    if action == 'idle':
        return [
            {'bob': 0, 'legs': 0, 'arms': 0, 'cape': 0},
            {'bob': 1, 'legs': 0, 'arms': 1, 'cape': 1},
            {'bob': 1, 'legs': 0, 'arms': 1, 'cape': 1},
            {'bob': 0, 'legs': 0, 'arms': 0, 'cape': 0},
        ]

    if action == 'walk':
        return [
            {'bob': 0, 'legs': 3, 'arms': -1, 'cape': 1},
            {'bob': 1, 'legs': 0, 'arms': 0, 'cape': 0},
            {'bob': 0, 'legs': -3, 'arms': 1, 'cape': 1},
            {'bob': 1, 'legs': 0, 'arms': 0, 'cape': 0},
        ]

    if action == 'run':
        return [
            {'bob': 0, 'legs': 5, 'arms': -2, 'lean': 1, 'cape': 3},
            {'bob': 2, 'legs': 1, 'arms': 0, 'lean': 1, 'cape': 4},
            {'bob': 0, 'legs': -5, 'arms': 2, 'lean': 1, 'cape': 3},
            {'bob': 2, 'legs': -1, 'arms': 0, 'lean': 1, 'cape': 4},
        ]

    if action == 'swipe':
        return [
            {'bob': 0, 'legs': 2, 'arms': -2, 'lean': -1, 'cape': 1},
            {'bob': 0, 'legs': 2, 'punch': 2, 'lean': 1, 'cape': 3},
            {'bob': 0, 'legs': 2, 'punch': 3, 'lean': 2, 'cape': 4},
            {'bob': 0, 'legs': 2, 'punch': 1, 'lean': 1, 'cape': 2},
        ]

    if action == 'lie':
        return [
            {'crouch': 7, 'legs': 0, 'arms': 2, 'cape': 0},
            {'crouch': 8, 'legs': 0, 'arms': 2, 'cape': 0},
        ]

    if action == 'land':
        return [
            {'crouch': 5, 'legs': 3, 'arms': 3, 'cape': 2},
            {'crouch': 2, 'legs': 1, 'arms': 1, 'cape': 1},
        ]

    # jump / fall_from_grab: knees up, arms overhead, cape streaming
    return [
        {'tuck': 1, 'reach': 1, 'bob': -1, 'cape': 4},
        {'tuck': 1, 'reach': 1, 'bob': 0, 'cape': 5},
    ]


ACTIONS = ['idle', 'walk', 'run', 'swipe', 'lie', 'land', 'jump', 'fall_from_grab']


# ---------------------------------------------------------------- GIF writing

def save_gif(frames, path):
    """Animated GIF with real transparency.

    We know every colour we drew, so the palette is built by hand with index 0
    reserved for transparent — far more reliable than quantising RGBA.
    """
    def pixels(img):
        raw = img.tobytes()
        return [tuple(raw[i:i + 4]) for i in range(0, len(raw), 4)]

    colours = []
    seen = {}
    for f in frames:
        for px in pixels(f):
            if px[3] < 128:
                continue
            rgb = px[:3]
            if rgb not in seen:
                seen[rgb] = len(colours) + 1      # 0 stays transparent
                colours.append(rgb)

    if len(colours) > 254:
        raise RuntimeError('too many colours for one sprite: %d' % len(colours))

    palette = [0, 0, 0]
    for c in colours:
        palette.extend(c)
    palette.extend([0] * (768 - len(palette)))

    out = []
    for f in frames:
        p = Image.new('P', f.size, 0)
        p.putpalette(palette)
        p.frombytes(bytes(0 if px[3] < 128 else seen[px[:3]] for px in pixels(f)))
        out.append(p.resize((f.width * SCALE, f.height * SCALE), Image.NEAREST))

    out[0].save(
        path,
        save_all=True,
        append_images=out[1:],
        duration=int(1000 / FPS),
        loop=0,
        transparency=0,
        disposal=2,
        optimize=False,
    )


def main():
    os.makedirs(OUT, exist_ok=True)
    made = 0

    for hero in HEROES:
        for action in ACTIONS:
            frames = [draw_hero(hero, pose) for pose in frames_for(hero, action)]
            save_gif(frames, os.path.join(OUT, '%s_%s_%dfps.gif' % (hero['id'], action, FPS)))
            made += 1

        # Picker icon: the first idle frame, trimmed to the character.
        icon = draw_hero(hero, frames_for(hero, 'idle')[0])
        box = icon.getbbox()
        icon = icon.crop(box).resize(
            ((box[2] - box[0]) * SCALE, (box[3] - box[1]) * SCALE), Image.NEAREST)
        icon.save(os.path.join(OUT, 'icon_%s.png' % hero['id']))

    # Default icon for the species tile in the picker.
    first = draw_hero(HEROES[0], frames_for(HEROES[0], 'idle')[0])
    box = first.getbbox()
    first.crop(box).resize(
        ((box[2] - box[0]) * SCALE, (box[3] - box[1]) * SCALE), Image.NEAREST
    ).save(os.path.join(OUT, 'icon.png'))

    print('wrote %d clips for %d heroes into %s' % (made, len(HEROES), OUT))


if __name__ == '__main__':
    main()
