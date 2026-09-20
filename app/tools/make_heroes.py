#!/usr/bin/env python3
"""Generate the original `hero` sprite pack into assets/pets/hero/.

Everything here is drawn from scratch as parametric pixel art: a shared chibi
body rig posed per frame, plus a per-character palette and a few silhouette
switches (cape, mask style, build). Nothing is traced from or derived from any
existing character art, so the output is yours to ship.

Draws at 32x36 logical pixels and upscales 4x with nearest-neighbour, which is
what gives it the hard-edged pixel look the rest of the pack has.

    python app/tools/make_heroes.py && python app/tools/gen-metrics.py

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
#        'visor'  wraparound band, lit edge to edge
#        'hood'   raised hood, the face sunk in shadow inside it
#        'domino' small mask, face shows
#        'lenses' full mask, two big pale lenses tapering outwards
#        'eared'  cowl with two pointed ears, jaw left bare below it
#        'none'   bare face
#
# The roster is picked for spread rather than for taste: each entry differs
# from every other in at least two of palette, mask and build, because at 26px
# on a card two heroes that share a silhouette and differ only in hue are not
# two heroes -- they are one hero the eye cannot tell apart.
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
    {
        # Slate rather than the black the name suggests, and the cape pushed
        # well below it. At a true near-black the suit and the cape were the
        # same colour at 26px: the figure read as a slab with a floating silver
        # belt, and the legs disappeared entirely. A dark hero still has to have
        # a silhouette inside its own shadow.
        'id': 'onyx', 'suit': (82, 86, 106), 'trim': (186, 192, 206),
        'skin': None, 'cape': (26, 28, 38), 'emblem': (214, 222, 240),
        'mask': 'cowl', 'build': 'bulky', 'eye': (150, 220, 255),
    },
    {
        'id': 'solar', 'suit': (244, 190, 52), 'trim': (255, 246, 214),
        'skin': None, 'cape': (236, 158, 40), 'emblem': (255, 252, 230),
        'mask': 'visor', 'build': 'normal', 'eye': (255, 240, 180),
    },
    {
        'id': 'abyss', 'suit': (28, 102, 116), 'trim': (96, 208, 206),
        'skin': None, 'cape': None, 'emblem': (150, 240, 236),
        'mask': 'helmet', 'build': 'slim', 'eye': (140, 246, 240),
    },
    {
        'id': 'sable', 'suit': (56, 60, 54), 'trim': (124, 142, 84),
        'skin': (224, 186, 150), 'cape': None, 'emblem': (170, 190, 120),
        'mask': 'hood', 'build': 'slim', 'eye': (236, 240, 220),
    },
    {
        'id': 'bronze', 'suit': (168, 104, 56), 'trim': (86, 166, 164),
        'skin': (238, 198, 160), 'cape': (140, 84, 44), 'emblem': (240, 214, 170),
        'mask': 'hood', 'build': 'bulky', 'eye': (255, 236, 196),
    },
    {
        'id': 'vermeil', 'suit': (198, 46, 120), 'trim': (255, 214, 140),
        'skin': None, 'cape': (168, 36, 102), 'emblem': (255, 236, 200),
        'mask': 'visor', 'build': 'normal', 'eye': (255, 230, 250),
    },
    {
        # Cowled and caped, in pink. Slim rather than normal on purpose: the
        # roster rule above wants two differences from every other entry, and
        # midnight already holds cowl+normal while onyx holds cowl+bulky, so
        # pink alone would not have been enough to tell this one apart.
        'id': 'rose', 'suit': (196, 74, 140), 'trim': (247, 168, 214),
        'skin': None, 'cape': (148, 44, 102), 'emblem': (255, 226, 244),
        'mask': 'cowl', 'build': 'slim', 'eye': (255, 255, 255),
    },
    {
        # Powered armour: red plate, gold faceplate, a lit core in the chest.
        # Bulky and visored -- solar and vermeil already hold visor+normal,
        # and crimson holds the red/gold palette with a helmet, so this needs
        # both the heaviest build and the full-width faceplate to be its own
        # figure rather than a recolour of one.
        'id': 'crucible', 'suit': (198, 46, 42), 'trim': (248, 190, 60),
        'skin': None, 'cape': None, 'emblem': (206, 250, 255),
        'mask': 'visor', 'build': 'bulky', 'eye': (226, 252, 255),
    },
    {
        # The acrobat: red over blue, full mask, big pale lenses. Slim because
        # the whole read is agility -- in a bulky build the lenses turn the
        # head into a headlamp and the figure stops looking quick.
        'id': 'cinnabar', 'suit': (206, 54, 50), 'trim': (56, 92, 178),
        'skin': None, 'cape': None, 'emblem': (232, 238, 250),
        'mask': 'lenses', 'build': 'slim', 'eye': (250, 252, 255),
    },
    {
        # The night vigilante: grey plate, black cowl and cape, a bright belt
        # and no emblem at all. Slate rather than black for the body, for the
        # reason onyx records -- at a true near-black the cape and the suit
        # are one shape and the figure has no silhouette inside its own
        # shadow. The belt is the only bright thing on it, which is what the
        # separate `belt` slot exists for: the boots stay dark.
        'id': 'nocturne', 'suit': (92, 96, 108), 'trim': (44, 46, 58),
        'skin': (226, 186, 150), 'cape': (28, 30, 40), 'emblem': None,
        'belt': (232, 200, 74),
        'mask': 'eared', 'build': 'bulky', 'eye': (238, 242, 250),
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
    # Belt. Its own colour when one is given, because the belt and the boots
    # are the same slot otherwise, and a figure wanting a bright belt over
    # dark boots could not say so.
    rect(d, cx - half_t, torso_b - 2, cx + half_t, torso_b, hero.get('belt') or trim)
    # Emblem, unless the character is defined by not having one.
    if hero['emblem']:
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
    elif mask == 'visor':
        # A band that runs edge to edge, unlike the helmet's inset slit. At this
        # size that full width is the whole difference between the two reading
        # as different headgear rather than as the same helmet in another colour.
        rect(d, cx - hw, head_t, cx + hw, head_b, suit)
        rect(d, cx - hw, head_t, cx + hw, head_t + 2, shade(suit, 1.15))
        rect(d, cx - hw, head_t + 3, cx + hw, head_t + 6, trim)
        rect(d, cx - hw, head_t + 4, cx + hw, head_t + 5, hero['eye'])
    elif mask == 'hood':
        # The hood is a pixel proud of the head on every side, which is what
        # makes it a hood rather than a helmet: the silhouette is bigger than
        # the skull inside it, and the face sits back in its shadow.
        rect(d, cx - hw - 1, head_t - 1, cx + hw + 1, head_b, trim)
        rect(d, cx - hw - 1, head_t - 1, cx + hw + 1, head_t + 1, shade(trim, 1.22))
        rect(d, cx - hw + 1, head_t + 3, cx + hw - 1, head_b - 1, shade(skin, 0.42))
        rect(d, cx - hw + 2, head_t + 4, cx - 1, head_t + 5, hero['eye'])
        rect(d, cx + 1, head_t + 4, cx + hw - 2, head_t + 5, hero['eye'])
    elif mask == 'eared':
        # A cowl that stops at the cheekbones with two ears standing off it.
        # The bare jaw is doing as much work as the ears: a full-face cowl in
        # this palette is a dark oval, and the strip of skin under it is what
        # makes the top half read as a mask worn over a face rather than as
        # the whole head.
        # The ears are carved out of the head's own height, not added on top
        # of it. There is nothing on top: at this build the skull already
        # starts two pixels below the top of a 36px canvas, so ears drawn
        # above it were simply clipped off and came out as two faint nubs.
        # The skull is dropped instead and the freed space becomes the ears,
        # which is the only way to get a real pair inside the frame.
        jaw_top = head_b - 3
        skull_top = jaw_top - 5
        ear_apex = max(0, skull_top - 6)

        # The rig lays down a full head in `skin` before this switch runs, and
        # this mask does not cover all of it -- the dropped skull leaves the
        # top of that fill showing as a band of bare forehead above the eyes.
        # Clear it back to nothing so only the ears break the outline. Safe to
        # erase: the cape starts at the shoulders, below the whole head.
        d.rectangle([cx - hw, head_t, cx + hw, skull_top - 1], fill=(0, 0, 0, 0))

        # Tall and thin, hard against the outer edges, with a wide flat gap
        # between them: the gap is as much of the shape as the ears are.
        for sx in (-1, 1):
            inner = cx + sx * (hw - 3)
            outer = cx + sx * hw
            apex = cx + sx * (hw - 1)
            d.polygon([(inner, skull_top + 1), (apex, ear_apex), (outer, skull_top + 1)],
                      fill=trim)

        rect(d, cx - hw, skull_top, cx + hw, jaw_top, trim)
        rect(d, cx - hw, skull_top, cx + hw, skull_top, shade(trim, 1.2))
        # The jaw, inset either side so it is a chin and not a stripe across
        # the whole head.
        rect(d, cx - hw + 2, jaw_top, cx + hw - 2, head_b, skin)
        rect(d, cx - hw + 1, skull_top + 2, cx - 1, skull_top + 3, hero['eye'])
        rect(d, cx + 1, skull_top + 2, cx + hw - 1, skull_top + 3, hero['eye'])
        rect(d, cx - 2, head_b - 1, cx + 2, head_b - 1, shade(skin, 0.74))

    elif mask == 'lenses':
        # Two big pale lenses over a full mask. They taper outwards -- tall at
        # the nose, shallow at the temple -- which is what keeps them reading
        # as a pair of lenses instead of as one visor band with a notch in it,
        # and the notch is the only thing separating them at this size.
        rect(d, cx - hw, head_t, cx + hw, head_b, suit)
        rect(d, cx - hw, head_t, cx + hw, head_t + 1, shade(suit, 1.18))
        for sx in (-1, 1):
            inner = cx + sx
            outer = cx + sx * (hw - 1)
            rect(d, inner, head_t + 3, outer, head_t + 5, hero['eye'])
            # shave the outer top corner so the lens leans back
            rect(d, outer, head_t + 3, outer, head_t + 3, suit)
            # a dark rim under each lens, or they float
            rect(d, inner, head_t + 6, outer, head_t + 6, shade(suit, 0.6))
        rect(d, cx, head_t + 3, cx, head_t + 6, shade(suit, 0.6))

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
