#!/usr/bin/env python3
"""Generate the original `fairy` sprite pack into assets/pets/fairy/.

The third pack built on the shared rig, after the heroes and the warriors, and
the first that needs the body to grow parts the rig has never had: wings behind
the shoulders, a pointed-ear hood, a flared tutu and a wand with a star on it.

Like the other two it is drawn from scratch as parametric pixel art and traces
nothing. The pointed hood, the wings and the wand are generic fairy-tale
furniture; there is no emblem, no name and no logo borrowed from anywhere.

The frame poses, the outliner and the GIF writer come from make_heroes, so
these animate on exactly the same cycle as every other pet. The pose key
`cape`, which the hero rig uses for cape flare, doubles here as the wing beat:
it already rises through a run and settles at rest, which is the motion wings
want anyway.

    python app/tools/make_fairies.py && python app/tools/gen-metrics.py

Needs Pillow (pip install Pillow).
"""

import os
import sys

try:
    from PIL import Image, ImageDraw
except ImportError:
    sys.exit("Pillow is required: pip install Pillow")

from make_heroes import (
    ACTIONS, FLOOR, FPS, H, SCALE, frames_for, outline, rect, save_gif, shade,
)

# Wide even by the warriors' standards: the wings reach further from the spine
# than a shield does, and clipped wings read as a lumpy back rather than as
# wings. Canvas width is free -- gen-metrics measures the opaque box.
W = 46

OUT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..', 'assets', 'pets', 'fairy')


# ---------------------------------------------------------------- characters

# Variants here are palette only, which is the exception the other two packs
# do not make. It is allowed because the silhouette is doing so much work by
# itself: ears, wings, wand and tutu are unmistakable at any size, so two of
# these differing only in hue still read as two of the same creature rather
# than as one creature drawn twice -- which is exactly what they are.
FAIRIES = [
    {
        # The pink one, and the reason the pack exists. Named blossom rather
        # than rose because the hero pack already has a pink `rose`, and two
        # pets listed as "Rose" differing only by species is a puzzle for
        # whoever reads the roster later.
        'id': 'blossom',
        'suit': (198, 92, 150), 'trim': (232, 146, 190), 'skin': (244, 196, 220),
        'cape': (104, 40, 78), 'wing': (236, 158, 200), 'accent': (255, 226, 242),
        'legs': (92, 36, 68), 'eye': (72, 26, 54),
        'build': 'slim', 'wings': False,
    },
    {
        # The pink one again in black and grey, and wingless with it. The
        # cape is kept well clear of the suit in value: the hero pack records
        # what happens when a dark figure's cape matches its body at this
        # size -- the two merge and the whole thing reads as one slab with no
        # silhouette inside its own shadow.
        'id': 'nightshade',
        'suit': (74, 78, 90), 'trim': (146, 152, 168), 'skin': (198, 204, 216),
        'cape': (26, 28, 36), 'wing': (120, 126, 142), 'accent': (222, 228, 240),
        # Legs a clear step above the cape, not level with it: at near-black
        # for both, the cape and the legs in front of it came out as one dark
        # trapezoid and the figure lost its legs below the hem.
        'legs': (62, 66, 80), 'eye': (22, 24, 32),
        'build': 'slim', 'wings': False,
    },
]

BUILD = {
    'slim':   {'torso': 4, 'limb': 1, 'head': 5},
    'normal': {'torso': 5, 'limb': 2, 'head': 6},
}


# ---------------------------------------------------------------- the parts

def draw_wings(d, f, cx, torso_t, half_t, beat):
    """Two lobes a side, behind everything.

    Big is the whole point. Drawn small first and they clustered round the
    torso as four indistinct blobs; a wing has to be a clear span wider than
    the body it is attached to before the eye calls it a wing. Upper lobe
    long and reaching up, lower lobe shorter and tucked under.

    `beat` spreads the pair: 0 at rest, 4 or 5 through a run.
    """
    wing, edge = f['wing'], shade(f['wing'], 0.62)
    lit = shade(f['wing'], 1.16)
    root = half_t - 1

    for sx in (-1, 1):
        # Chest height, not head to ankle. Sized up from tiny to enormous and
        # then back: at full height they rose past the ears and fell past the
        # hem, and the figure stopped being a fairy with wings and became a
        # pair of wings with a face in the middle. They span the torso, and
        # the head above and the tutu and legs below stay clear of them.
        ux1 = cx + sx * (root + 2)
        ux2 = cx + sx * (root + 16 + beat)
        utop = torso_t - 1 - beat
        d.ellipse([min(ux1, ux2), utop, max(ux1, ux2), utop + 12],
                  fill=wing, outline=edge)

        lx1 = cx + sx * (root + 1)
        lx2 = cx + sx * (root + 11 + beat // 2)
        ltop = torso_t + 9
        d.ellipse([min(lx1, lx2), ltop, max(lx1, lx2), ltop + 9],
                  fill=shade(wing, 0.84), outline=edge)

        # A lit streak along the outer edge of the upper lobe. Without it the
        # two lobes touch and merge back into the blob this is avoiding.
        ox = cx + sx * (root + 10 + beat)
        d.ellipse([min(ox, ox + sx * 4), utop + 2, max(ox, ox + sx * 4), utop + 6],
                  fill=lit)


def draw_wand(d, f, cx, torso_t, half_t, half_l, punch, swing):
    """A stick with a star on it, in the front hand."""
    hx = cx + half_t + half_l + 3 + punch
    hy = torso_t + (4 if punch else 5 + swing)
    star = f['accent']

    rect(d, hx, hy - 9, hx + 1, hy + 3, f['trim'])
    # Four spokes and a centre: a plus sign with the diagonals knocked off,
    # which at this size is the only star that does not turn into a blob.
    sy = hy - 13
    rect(d, hx - 2, sy, hx + 3, sy + 1, star)
    rect(d, hx, sy - 2, hx + 1, sy + 3, star)
    d.point((hx - 1, sy - 1), fill=star)
    d.point((hx + 2, sy - 1), fill=star)
    d.point((hx - 1, sy + 2), fill=star)
    d.point((hx + 2, sy + 2), fill=star)


def draw_hood(d, f, cx, head_t, head_b, hw):
    """Pointed-ear hood: the head, plus an ear standing off each top corner."""
    trim, skin = f['trim'], f['skin']

    # Ears first, so the hood's own shading runs over their base. They stand
    # UP with only a slight outward lean -- angled out from the head they
    # stopped being ears and became horns.
    for sx in (-1, 1):
        inner = cx + sx * (hw - 4)
        outer = cx + sx * hw
        apex = cx + sx * (hw - 1)
        d.polygon([(inner, head_t + 1), (apex, head_t - 9), (outer, head_t + 1)],
                  fill=trim)
        d.polygon([(apex, head_t - 7), (apex + sx, head_t - 1), (outer, head_t)],
                  fill=shade(trim, 0.62))

    rect(d, cx - hw, head_t, cx + hw, head_b, trim)
    rect(d, cx - hw, head_t, cx + hw, head_t + 1, shade(trim, 1.18))
    # the face, sunk inside the hood
    rect(d, cx - hw + 1, head_t + 3, cx + hw - 1, head_b - 1, skin)
    rect(d, cx - hw + 1, head_t + 4, cx - 1, head_t + 5, f['eye'])
    rect(d, cx + 1, head_t + 4, cx + hw - 1, head_t + 5, f['eye'])
    # mouth, one pixel, which is all there is room for
    rect(d, cx - 1, head_b - 2, cx + 1, head_b - 2, shade(skin, 0.78))


def draw_fairy(f, pose):
    """One frame. `pose` carries the offsets that make it an animation."""
    img = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    b = BUILD[f['build']]
    suit, trim = f['suit'], f['trim']
    dark = f['legs']

    cx = W // 2 + pose.get('lean', 0)
    bob = pose.get('bob', 0)
    crouch = pose.get('crouch', 0)
    tuck = pose.get('tuck', 0)

    foot_y = FLOOR
    leg_top = 23 + crouch + bob
    torso_b = leg_top + 1
    torso_t = torso_b - (11 - crouch // 2)
    head_b = torso_t + 1
    head_t = head_b - (b['head'] * 2)

    half_t = b['torso']
    half_l = b['limb']
    swing = pose.get('arms', 0)
    beat = pose.get('cape', 0)

    # ---- wings, or a cape where the wings would have been ----
    #
    # Wingless is not a stripped-down fairy, it is a different figure: with
    # the wings gone there is finally room behind the shoulders for a cape to
    # hang and be seen, which is the whole reason the cape became a mantle
    # when the wings were there.
    if f.get('wings', True):
        draw_wings(d, f, cx, torso_t, half_t, beat)
    else:
        cloak = f['cape']
        d.polygon(
            [
                (cx - half_t - 2, torso_t),
                (cx + half_t + 2, torso_t),
                (cx + half_t + 5 + beat, foot_y - 3),
                (cx + beat // 2, foot_y - 6),
                (cx - half_t - 5 + beat, foot_y - 3),
            ],
            fill=cloak,
        )
        d.polygon(
            [(cx - half_t - 2, torso_t), (cx, torso_t + 3), (cx + half_t + 2, torso_t)],
            fill=shade(cloak, 1.3),
        )

    # ---- legs ----
    if tuck:
        for sx in (-1, 1):
            x0 = cx + sx * 1
            rect(d, x0 - half_l, leg_top, x0 + half_l + sx * 3, leg_top + 4, dark)
            rect(d, x0 + sx * 2, leg_top + 3, x0 + sx * 5, leg_top + 6, trim)
    else:
        lswing = pose.get('legs', 0)
        for sx, off in ((-1, -lswing), (1, lswing)):
            x0 = cx + sx * (half_l + 1) + off
            rect(d, x0 - half_l, leg_top, x0 + half_l, foot_y - 3, dark)
            rect(d, x0 - half_l - 1, foot_y - 3, x0 + half_l + 1, foot_y, shade(dark, 1.5))

    # ---- tutu: wider than anything else on the figure ----
    if not tuck:
        # Sits at the hip and in front of the lower wing lobes -- drawn above
        # them and it disappeared between the two.
        ty = leg_top - 2
        rect(d, cx - half_t - 3, ty - 1, cx + half_t + 3, ty, f['accent'])
        rect(d, cx - half_t - 7, ty, cx + half_t + 7, ty + 2, f['accent'])
        rect(d, cx - half_t - 7, ty, cx + half_t + 7, ty, shade(f['accent'], 1.06))
        # a scalloped hem, so it is a tutu and not a plank
        for k in range(-half_t - 7, half_t + 8, 3):
            rect(d, cx + k, ty + 3, cx + k + 1, ty + 3, shade(f['accent'], 0.82))

    # ---- torso ----
    rect(d, cx - half_t, torso_t, cx + half_t, torso_b, suit)
    rect(d, cx - half_t, torso_t, cx - half_t + 1, torso_b, shade(suit, 1.2))
    rect(d, cx + half_t - 1, torso_t, cx + half_t, torso_b, shade(suit, 0.82))
    rect(d, cx - half_t, torso_b - 2, cx + half_t, torso_b, trim)

    # A mantle across the shoulders, in the darkest colour the figure owns.
    # This started as a full cape hanging down the back, which was invisible:
    # the wings are behind it and the torso is in front of it, so every pixel
    # of it was covered. Over the shoulders it has somewhere to be seen, and
    # it is what separates the head from the wings.
    cape = f['cape']
    rect(d, cx - half_t - 1, torso_t, cx + half_t + 1, torso_t + 2, cape)
    rect(d, cx - half_t - 1, torso_t, cx + half_t + 1, torso_t, shade(cape, 1.35))

    # a little collar, where a hero would carry an emblem
    rect(d, cx - 2, torso_t + 3, cx + 2, torso_t + 4, f['accent'])

    # ---- arms ----
    punch = pose.get('punch', 0)
    bx = cx - half_t - half_l - 1
    rect(d, bx - half_l, torso_t + 1 - swing, bx + half_l, torso_b - 2 - swing, shade(suit, 0.86))

    if punch:
        ay = torso_t + 3
        fx = cx + half_t
        rect(d, fx, ay, fx + 4 + punch, ay + half_l * 2, suit)
    elif pose.get('reach'):
        for sx in (-1, 1):
            ax = cx + sx * (half_t + half_l + 1)
            rect(d, ax - half_l, torso_t - 5, ax + half_l, torso_t + 3, suit)
    else:
        fx = cx + half_t + half_l + 1
        rect(d, fx - half_l, torso_t + 1 + swing, fx + half_l, torso_b - 2 + swing, suit)

    # ---- wand ----
    if not pose.get('reach'):
        draw_wand(d, f, cx, torso_t, half_t, half_l, punch, swing)

    # ---- head ----
    draw_hood(d, f, cx, head_t, head_b, b['head'])

    return outline(img)


def main():
    os.makedirs(OUT, exist_ok=True)
    made = 0

    for f in FAIRIES:
        for action in ACTIONS:
            frames = [draw_fairy(f, pose) for pose in frames_for(f, action)]
            save_gif(frames, os.path.join(OUT, '%s_%s_%dfps.gif' % (f['id'], action, FPS)))
            made += 1

        icon = draw_fairy(f, frames_for(f, 'idle')[0])
        box = icon.getbbox()
        icon.crop(box).resize(
            ((box[2] - box[0]) * SCALE, (box[3] - box[1]) * SCALE), Image.NEAREST
        ).save(os.path.join(OUT, 'icon_%s.png' % f['id']))

    first = draw_fairy(FAIRIES[0], frames_for(FAIRIES[0], 'idle')[0])
    box = first.getbbox()
    first.crop(box).resize(
        ((box[2] - box[0]) * SCALE, (box[3] - box[1]) * SCALE), Image.NEAREST
    ).save(os.path.join(OUT, 'icon.png'))

    print('wrote %d clips for %d fairies into %s' % (made, len(FAIRIES), OUT))


if __name__ == '__main__':
    main()
