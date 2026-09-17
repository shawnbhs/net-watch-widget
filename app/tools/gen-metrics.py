#!/usr/bin/env python3
"""Generate assets/pets/metrics.json.

The sprite clips do not share a canvas size or padding: the akita dog's idle
frame is 174x115 while its walk frame is 105x85, and totoro's run clip is a
500x238 catbus. Scaling each clip to a fixed height therefore makes a pet grow
and shrink as it switches animation, and its feet drift off whatever it is
standing on.

So we measure the actual drawn character instead: for every clip, take the
union of the opaque bounding box across all of its frames. The renderer scales
by that box and anchors the pet by the box's bottom edge, which keeps a pet the
same size in every animation and plants its feet exactly on a card's edge.

Run after adding sprites:  python app/tools/gen-metrics.py
Needs Pillow (pip install Pillow). The output is committed, so nobody needs
Pillow just to run the widget.
"""

import json
import os
import sys

try:
    from PIL import Image
except ImportError:
    sys.exit("Pillow is required: pip install Pillow")

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'assets', 'pets')


def clip_metrics(path):
    """(canvas_w, canvas_h, box_x, box_y, box_w, box_h) or None if fully blank."""
    with Image.open(path) as im:
        cw, ch = im.size
        box = None
        for frame in range(getattr(im, 'n_frames', 1)):
            im.seek(frame)
            alpha = im.convert('RGBA').getchannel('A')
            b = alpha.getbbox()
            if b is None:
                continue
            box = b if box is None else (
                min(box[0], b[0]), min(box[1], b[1]),
                max(box[2], b[2]), max(box[3], b[3]),
            )

    if box is None:
        return [cw, ch, 0, 0, cw, ch]
    return [cw, ch, box[0], box[1], box[2] - box[0], box[3] - box[1]]


def main():
    out = {}
    for species in sorted(os.listdir(ROOT)):
        folder = os.path.join(ROOT, species)
        if not os.path.isdir(folder):
            continue
        for name in sorted(os.listdir(folder)):
            if not name.endswith('.gif'):
                continue
            key = species + '/' + name
            try:
                out[key] = clip_metrics(os.path.join(folder, name))
            except Exception as err:            # noqa: BLE001 - report and carry on
                print('  skipped %s (%s)' % (key, err))

    target = os.path.join(ROOT, 'metrics.json')
    with open(target, 'w', encoding='utf-8') as fh:
        json.dump(out, fh, separators=(',', ':'), sort_keys=True)

    print('wrote %s (%d clips)' % (target, len(out)))


if __name__ == '__main__':
    main()
