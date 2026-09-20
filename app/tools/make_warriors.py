#!/usr/bin/env python3
"""Generate the original `warrior` sprite pack into assets/pets/warrior/.

A companion to make_heroes.py and built on the same rig: 32x36 logical pixels,
upscaled 4x with nearest-neighbour, everything drawn from scratch as parametric
pixel art. Nothing is traced from or derived from any existing character art.

Where the heroes vary by palette, mask and build, these vary by *equipment* --
helm, shield, weapon, skirt -- because that is what tells a gladiator from a
knight at the size these are actually seen. The lesson the hero pack records
applies with more force here: at 26px on a card, two fighters that share a
silhouette and differ only in hue are one fighter. So no two entries below
share a helm, and the shield and weapon are chosen to break up the outline
differently in each.

The frame poses, the outliner and the GIF writer are imported from
make_heroes rather than copied: they are the part that is finicky and already
correct, and two copies of a palette-building GIF encoder would drift.

    python app/tools/make_warriors.py && python app/tools/gen-metrics.py

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

# Wider canvas than the heroes' 32. A hero is a body; one of these is a body
# with a shield out one side and a weapon out the other, and at 32 the two
# were clipped into the torso until neither read as a separate object. The
# extra width costs nothing downstream: gen-metrics.py measures the opaque box
# per clip and the engine scales by that, not by the canvas.
W = 40

OUT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..', 'assets', 'pets', 'warrior')


# ---------------------------------------------------------------- characters

# helm:   'galea'      crested front-to-back, cheek plates      (gladiator)
#         'transverse' crest running side to side, wide         (centurion)
#         'great'      flat-topped bucket, single visor slit    (knight)
#         'kabuto'     flared neck guard, crescent at the brow  (samurai)
#         'horned'     round cap, nasal bar, a horn each side   (viking)
#         'nemes'      striped headdress flaring to the shoulders (pharaoh)
#
# shield: None | 'round' | 'kite' | 'scutum'
# weapon: None | 'sword' | 'spear' | 'axe' | 'crook'
WARRIORS = [
    {
        'id': 'gladiator',
        'armour': (138, 92, 42), 'trim': (238, 202, 110), 'skin': (226, 176, 128),
        'cloth': (162, 62, 52), 'accent': (255, 236, 176),
        'helm': 'galea', 'shield': 'round', 'weapon': 'sword',
        'skirt': True, 'build': 'bulky', 'bare_arms': True, 'bare_legs': True,
        'eye': (40, 28, 22),
    },
    {
        'id': 'centurion',
        'armour': (198, 60, 52), 'trim': (240, 208, 116), 'skin': (226, 178, 132),
        'cloth': (150, 40, 38), 'accent': (255, 240, 190),
        'helm': 'transverse', 'shield': 'scutum', 'weapon': 'spear',
        'skirt': True, 'build': 'normal', 'bare_arms': False, 'bare_legs': False,
        'eye': (40, 28, 22),
    },
    {
        'id': 'knight',
        'armour': (168, 178, 196), 'trim': (86, 104, 150), 'skin': None,
        'cloth': (62, 82, 146), 'accent': (226, 236, 252),
        'helm': 'great', 'shield': 'kite', 'weapon': 'sword',
        'skirt': False, 'build': 'bulky', 'bare_arms': False, 'bare_legs': False,
        'eye': (24, 30, 44),
    },
    {
        'id': 'samurai',
        'armour': (58, 58, 72), 'trim': (188, 52, 60), 'skin': (230, 186, 142),
        'cloth': (34, 36, 46), 'accent': (240, 206, 110),
        'helm': 'kabuto', 'shield': None, 'weapon': 'sword',
        'skirt': True, 'build': 'normal', 'bare_arms': False, 'bare_legs': False,
        'eye': (32, 26, 26),
    },
    {
        'id': 'viking',
        'armour': (122, 92, 62), 'trim': (176, 166, 150), 'skin': (232, 186, 146),
        'cloth': (96, 70, 46), 'accent': (226, 216, 196),
        'helm': 'horned', 'shield': 'round', 'weapon': 'axe',
        'skirt': False, 'build': 'bulky', 'bare_arms': True, 'bare_legs': False,
        'eye': (60, 100, 150),
    },
    {
        'id': 'pharaoh',
        'armour': (54, 102, 166), 'trim': (238, 198, 72), 'skin': (206, 144, 90),
        'cloth': (242, 228, 194), 'accent': (255, 244, 206),
        'helm': 'nemes', 'shield': None, 'weapon': 'crook',
        'skirt': True, 'build': 'slim', 'bare_arms': True, 'bare_legs': True,
        'eye': (34, 26, 22),
    },
]

# Slightly heavier than the hero rig: these carry shields and shoulder plates,
# and a fighter with the build of a runner reads as a hero holding a prop.
# Same widths as the hero rig. Going wider to suggest armour was a mistake:
# it did not read as heavier, it read as a bigger rectangle, and it left no
# room either side for the equipment that actually distinguishes these.
BUILD = {
    'slim':   {'torso': 4, 'limb': 1, 'head': 5},
    'normal': {'torso': 5, 'limb': 2, 'head': 6},
    'bulky':  {'torso': 6, 'limb': 3, 'head': 6},
}


# ---------------------------------------------------------------- the rig

def draw_helm(d, w, cx, head_t, head_b, hw):
    """The head, and the thing on it.

    Each branch has to change the *outline*, not just fill it differently.
    Where a helm only recolours the skull it is drawn a pixel or two proud of
    it instead, or given something that sticks out -- a crest, a horn, a flare
    -- so the shape survives being 26 pixels tall on a card.
    """
    armour, trim, accent = w['armour'], w['trim'], w['accent']
    skin = w['skin'] or armour
    helm = w['helm']

    if helm == 'galea':
        # Crest front to back: a ridge along the top, so from the side the
        # head is twice as tall as it is wide.
        rect(d, cx - hw, head_t, cx + hw, head_b, armour)
        rect(d, cx - hw, head_t, cx + hw, head_t + 1, shade(armour, 1.2))
        rect(d, cx - 2, head_t - 6, cx + 2, head_t, w['cloth'])
        rect(d, cx - 2, head_t - 6, cx, head_t - 1, shade(w['cloth'], 1.32))
        rect(d, cx - 3, head_t - 3, cx + 3, head_t - 1, w['cloth'])
        # cheek plates leave a vertical strip of face open
        rect(d, cx - hw, head_t + 3, cx - hw + 1, head_b, shade(armour, 0.8))
        rect(d, cx + hw - 1, head_t + 3, cx + hw, head_b, shade(armour, 0.8))
        rect(d, cx - hw + 2, head_t + 3, cx + hw - 2, head_b - 1, skin)
        rect(d, cx - hw + 2, head_t + 4, cx - 1, head_t + 5, w['eye'])
        rect(d, cx + 1, head_t + 4, cx + hw - 2, head_t + 5, w['eye'])

    elif helm == 'transverse':
        # The same Roman helm turned 90 degrees: the crest is a wide bar, so
        # the silhouette is broad where the gladiator's is tall.
        rect(d, cx - hw, head_t, cx + hw, head_b, armour)
        rect(d, cx - hw - 2, head_t - 3, cx + hw + 2, head_t - 1, w['cloth'])
        rect(d, cx - hw - 2, head_t - 3, cx + hw + 2, head_t - 3, shade(w['cloth'], 1.3))
        rect(d, cx - hw, head_t, cx + hw, head_t + 1, shade(armour, 1.2))
        rect(d, cx - hw + 1, head_t + 3, cx + hw - 1, head_b - 1, skin)
        rect(d, cx - hw + 1, head_t + 4, cx - 1, head_t + 5, w['eye'])
        rect(d, cx + 1, head_t + 4, cx + hw - 1, head_t + 5, w['eye'])

    elif helm == 'great':
        # A bucket: flat top, straight sides, one slit. No face at all, which
        # is the whole of its character.
        rect(d, cx - hw - 1, head_t - 1, cx + hw + 1, head_b + 1, armour)
        rect(d, cx - hw - 1, head_t - 1, cx + hw + 1, head_t, shade(armour, 1.22))
        rect(d, cx - hw - 1, head_t + 4, cx + hw + 1, head_t + 5, shade(armour, 0.34))
        rect(d, cx - 1, head_t + 6, cx + 1, head_b + 1, shade(armour, 0.5))
        rect(d, cx - hw, head_b, cx + hw, head_b + 1, trim)

    elif helm == 'kabuto':
        # Wide flared neck guard below, crescent above: two horizontals that
        # nothing else in the set has.
        rect(d, cx - hw, head_t + 1, cx + hw, head_b, armour)
        rect(d, cx - hw - 2, head_b - 3, cx + hw + 2, head_b - 1, shade(armour, 0.86))
        rect(d, cx - hw - 3, head_b - 2, cx + hw + 3, head_b - 1, shade(armour, 0.7))
        rect(d, cx - hw, head_t + 1, cx + hw, head_t + 2, shade(armour, 1.2))
        # maedate: a crescent standing off the brow
        rect(d, cx - 3, head_t - 2, cx + 3, head_t - 1, accent)
        rect(d, cx - 4, head_t - 1, cx - 3, head_t, accent)
        rect(d, cx + 3, head_t - 1, cx + 4, head_t, accent)
        rect(d, cx - hw + 1, head_t + 4, cx + hw - 1, head_b - 3, skin)
        rect(d, cx - hw + 1, head_t + 4, cx - 1, head_t + 5, w['eye'])
        rect(d, cx + 1, head_t + 4, cx + hw - 1, head_t + 5, w['eye'])

    elif helm == 'horned':
        # Round cap with a nasal bar, and a horn out each side -- the widest
        # head in the set, and the only one broken by its own beard.
        rect(d, cx - hw, head_t + 1, cx + hw, head_b, skin)
        rect(d, cx - hw, head_t + 1, cx + hw, head_t + 4, trim)
        rect(d, cx - hw, head_t + 1, cx + hw, head_t + 1, shade(trim, 1.25))
        rect(d, cx - 1, head_t + 4, cx, head_t + 7, shade(trim, 0.8))   # nasal
        for sx in (-1, 1):
            rect(d, cx + sx * (hw + 1), head_t + 1, cx + sx * (hw + 2), head_t + 2, w['accent'])
            rect(d, cx + sx * (hw + 2), head_t - 1, cx + sx * (hw + 3), head_t + 1, w['accent'])
        rect(d, cx - hw + 1, head_t + 5, cx - 2, head_t + 6, w['eye'])
        rect(d, cx + 2, head_t + 5, cx + hw - 1, head_t + 6, w['eye'])
        rect(d, cx - hw + 1, head_b - 2, cx + hw - 1, head_b, shade(w['cloth'], 1.1))  # beard

    else:  # nemes
        # Flares outward as it descends, so the head is a trapezoid -- no other
        # entry widens towards the shoulders.
        rect(d, cx - hw, head_t + 1, cx + hw, head_b, skin)
        rect(d, cx - hw, head_t, cx + hw, head_t + 3, w['armour'])
        rect(d, cx - hw - 1, head_t + 3, cx + hw + 1, head_b - 2, w['armour'])
        rect(d, cx - hw - 2, head_b - 2, cx + hw + 2, head_b + 1, w['armour'])
        for k in range(-1, 2):
            rect(d, cx + k * 3, head_t + 3, cx + k * 3, head_b, w['trim'])
        rect(d, cx - hw + 2, head_t + 4, cx + hw - 2, head_b - 1, skin)
        rect(d, cx - hw + 2, head_t + 5, cx - 1, head_t + 6, w['eye'])
        rect(d, cx + 1, head_t + 5, cx + hw - 2, head_t + 6, w['eye'])
        # uraeus
        rect(d, cx, head_t + 1, cx, head_t + 2, w['trim'])


def draw_shield(d, w, cx, torso_t, torso_b, half_t, swing):
    """Strapped to the back forearm, and drawn *over* it.

    Drawn behind the arm first, which put the arm straight through the middle
    of it: all that showed was a two-pixel crescent poking out to the left,
    and a shield you cannot see is just a wider silhouette. It is held on the
    arm, so it covers the arm.
    """
    kind = w['shield']
    if not kind:
        return
    # `cloth`, not `trim`: on the knight the trim is a grey a shade off the
    # armour, and the shield vanished into the chest it was held against.
    face, edge = w['cloth'], shade(w['cloth'], 0.55)
    # A clear pixel of gap between shield and torso: touching, the outliner
    # wraps the two as one shape and the shield becomes a bulge in the chest.
    x = cx - half_t - 10
    y = torso_t - 1 - swing

    if kind == 'round':
        d.ellipse([x, y, x + 8, y + 10], fill=face, outline=edge)
        d.ellipse([x + 3, y + 4, x + 5, y + 6], fill=w['accent'])
    elif kind == 'kite':
        d.polygon(
            [(x + 1, y), (x + 8, y), (x + 8, y + 7), (x + 4, y + 13), (x + 1, y + 7)],
            fill=face, outline=edge)
        rect(d, x + 4, y + 2, x + 5, y + 10, w['accent'])
    else:  # scutum: a tall rectangle, curled edges picked out in shadow
        rect(d, x + 1, y - 1, x + 8, y + 13, face)
        rect(d, x + 1, y - 1, x + 2, y + 13, edge)
        rect(d, x + 7, y - 1, x + 8, y + 13, edge)
        rect(d, x + 4, y + 5, x + 5, y + 7, w['accent'])


def draw_weapon(d, w, cx, torso_t, half_t, half_l, punch, swing):
    """Held in the front hand. Extends the silhouette to the right."""
    kind = w['weapon']
    if not kind:
        return
    steel, grip = w['accent'], shade(w['cloth'], 0.6)
    hx = cx + half_t + half_l + 3 + punch
    hy = torso_t + (4 if punch else 5 + swing)

    if kind == 'sword':
        # Blade above the fist, guard across it: two pixels of crossbar are
        # what stop it reading as a stick.
        rect(d, hx, hy - 12, hx + 1, hy - 1, steel)
        rect(d, hx, hy - 12, hx, hy - 1, shade(steel, 1.15))
        rect(d, hx - 2, hy - 1, hx + 3, hy, w['trim'])
        rect(d, hx, hy, hx + 1, hy + 3, grip)
    elif kind == 'spear':
        rect(d, hx, hy - 14, hx + 1, hy + 5, grip)
        d.polygon(
            [(hx - 1, hy - 14), (hx + 1, hy - 19), (hx + 3, hy - 14)], fill=steel)
    elif kind == 'axe':
        rect(d, hx, hy - 12, hx + 1, hy + 4, grip)
        # A crescent head: wide at the edge, narrow at the haft.
        d.polygon(
            [(hx + 1, hy - 12), (hx + 7, hy - 11), (hx + 8, hy - 7),
             (hx + 7, hy - 3), (hx + 1, hy - 4)], fill=steel)
        rect(d, hx + 1, hy - 12, hx + 2, hy - 4, shade(steel, 0.7))
    else:  # crook
        rect(d, hx, hy - 11, hx + 1, hy + 4, steel)
        d.polygon(
            [(hx, hy - 15), (hx + 4, hy - 15), (hx + 4, hy - 11),
             (hx + 3, hy - 11), (hx + 3, hy - 14), (hx, hy - 14)], fill=steel)


def draw_warrior(w, pose):
    """One frame. `pose` carries the offsets that make it an animation."""
    img = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    b = BUILD[w['build']]
    armour, trim = w['armour'], w['trim']
    skin = w['skin'] or armour
    dark = shade(armour, 0.62)

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

    # ---- legs ----
    # Dark leg, bright boot -- not the other way round. Drawn the other way
    # round first, and a bare-legged fighter in tan armour came out as one
    # unbroken slab from collar to ankle with no legs in it at all.
    leg = skin if w.get('bare_legs') else dark
    if tuck:
        for sx in (-1, 1):
            x0 = cx + sx * 1
            rect(d, x0 - half_l, leg_top, x0 + half_l + sx * 3, leg_top + 4, leg)
            rect(d, x0 + sx * 2, leg_top + 3, x0 + sx * 5, leg_top + 6, trim)
    else:
        lswing = pose.get('legs', 0)
        for sx, off in ((-1, -lswing), (1, lswing)):
            x0 = cx + sx * (half_l + 1) + off
            rect(d, x0 - half_l, leg_top, x0 + half_l, foot_y - 3, leg)
            rect(d, x0 - half_l - 1, foot_y - 3, x0 + half_l + 1, foot_y, trim)

    # ---- skirt: pteruges, drawn over the top of the legs ----
    if w['skirt'] and not tuck:
        # Short, and in cloth rather than armour: it has to separate the
        # cuirass from the legs, so matching either defeats it.
        sy = leg_top - 1
        rect(d, cx - half_t, sy, cx + half_t, sy + 2, w['cloth'])
        rect(d, cx - half_t, sy, cx + half_t, sy, shade(w['cloth'], 1.25))
        for k in range(-half_t, half_t + 1, 2):
            rect(d, cx + k, sy + 3, cx + k, sy + 4, shade(w['cloth'], 0.78))

    # ---- torso ----
    rect(d, cx - half_t, torso_t, cx + half_t, torso_b, armour)
    rect(d, cx - half_t, torso_t, cx - half_t + 1, torso_b, shade(armour, 1.2))
    rect(d, cx + half_t - 1, torso_t, cx + half_t, torso_b, shade(armour, 0.82))
    # Pauldrons at the corners rather than a bar across the whole chest: the
    # bar made every fighter look like the same wide rectangle.
    rect(d, cx - half_t, torso_t, cx - half_t + 2, torso_t + 1, trim)
    rect(d, cx + half_t - 2, torso_t, cx + half_t, torso_t + 1, trim)
    rect(d, cx - half_t, torso_b - 2, cx + half_t, torso_b, trim)
    rect(d, cx - 1, torso_t + 3, cx + 1, torso_t + 5, w['accent'])

    # ---- arms ----
    punch = pose.get('punch', 0)
    arm = skin if w['bare_arms'] else armour

    bx = cx - half_t - half_l - 1
    rect(d, bx - half_l, torso_t + 1 - swing, bx + half_l, torso_b - 2 - swing, shade(arm, 0.86))

    if punch:
        ay = torso_t + 3
        fx = cx + half_t
        rect(d, fx, ay, fx + 4 + punch, ay + half_l * 2, arm)
    elif pose.get('reach'):
        for sx in (-1, 1):
            ax = cx + sx * (half_t + half_l + 1)
            rect(d, ax - half_l, torso_t - 5, ax + half_l, torso_t + 3, arm)
    else:
        fx = cx + half_t + half_l + 1
        rect(d, fx - half_l, torso_t + 1 + swing, fx + half_l, torso_b - 2 + swing, arm)

    # ---- equipment, over the arms that carry it ----
    if not pose.get('reach'):
        draw_shield(d, w, cx, torso_t, torso_b, half_t, swing)
        draw_weapon(d, w, cx, torso_t, half_t, half_l, punch, swing)

    # ---- head ----
    draw_helm(d, w, cx, head_t, head_b, b['head'])

    return outline(img)


def main():
    os.makedirs(OUT, exist_ok=True)
    made = 0

    for w in WARRIORS:
        for action in ACTIONS:
            frames = [draw_warrior(w, pose) for pose in frames_for(w, action)]
            save_gif(frames, os.path.join(OUT, '%s_%s_%dfps.gif' % (w['id'], action, FPS)))
            made += 1

        icon = draw_warrior(w, frames_for(w, 'idle')[0])
        box = icon.getbbox()
        icon.crop(box).resize(
            ((box[2] - box[0]) * SCALE, (box[3] - box[1]) * SCALE), Image.NEAREST
        ).save(os.path.join(OUT, 'icon_%s.png' % w['id']))

    first = draw_warrior(WARRIORS[0], frames_for(WARRIORS[0], 'idle')[0])
    box = first.getbbox()
    first.crop(box).resize(
        ((box[2] - box[0]) * SCALE, (box[3] - box[1]) * SCALE), Image.NEAREST
    ).save(os.path.join(OUT, 'icon.png'))

    print('wrote %d clips for %d warriors into %s' % (made, len(WARRIORS), OUT))


if __name__ == '__main__':
    main()
