# Credits

## Sprites

Every pet sprite in `assets/pets/` comes from **[vscode-pets](https://github.com/tonybaloney/vscode-pets)**
by Anthony Shaw, redistributed here under its MIT licence. The artwork inside that
project is credited by its authors as follows:

| Assets | Artist |
|---|---|
| Dog | NVPH Studio |
| Clippy, Rocky, Zappy, Rubber duck, Snake, Cockatiel, Ferris the crab, Mod the dotnet bot | Marc Duiker |
| Fox | Elthen |
| Remaining species (chicken, horse, monkey, panda, raccoon, rat, skeleton, snail, totoro, turtle, deno, morph) | see the upstream vscode-pets repository |

**Cats are deliberately absent.** The author of the cat set asked that those
assets not be redistributed; they are sold separately as the
[catset](https://seethingswarm.itch.io/catset) on itch.io. Buying it and dropping
`<colour>_<action>_8fps.gif` files into `assets/pets/cat/` is enough — the sprite
scanner in `src/main.js` picks up any new folder automatically, no code change needed.

## The `hero` pack — original work

The six heroes in `assets/pets/hero/` are **not** third-party art. They are
generated from scratch by `tools/make_heroes.py`: a shared chibi body rig posed
frame by frame, plus a palette and a few silhouette switches (cape, mask style,
build) per character. Nothing is traced from, derived from, or based on any
existing character, so they carry no third-party licence at all — edit the
`HEROES` table and re-run the script to make your own.

**On licensed characters:** sprite sets for Marvel, DC and similar properties
are not available under any licence that permits bundling them here — what
circulates online is unlicensed fan art. If you own a sprite pack you are
entitled to use, PetRail will pick it up with no code change: drop the frames
into `assets/pets/<name>/` named `<colour>_<action>_8fps.gif`, add an
`icon.png`, and run `python tools/gen-metrics.py`.

## Type

Varela Round and Nunito Sans, both from Google Fonts (SIL Open Font Licence).
They are loaded over the network; PetRail falls back to Segoe UI Variable offline.
