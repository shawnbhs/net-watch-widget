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
`<colour>_<action>_8fps.gif` files into `app/assets/pets/cat/` is enough — the
sprite scanner in `app/electron/pets.js` picks up any new folder automatically,
no code change needed.

## The `hero` pack — original work

The twelve heroes in `app/assets/pets/hero/` are **not** third-party art. They
are generated from scratch by `app/tools/make_heroes.py`: a shared chibi body rig
posed frame by frame, plus a palette and a few silhouette switches (cape, mask
style, build) per character. Nothing is traced from, derived from, or based on
any existing character, so they carry no third-party licence at all — edit the
`HEROES` table and re-run the script to make your own.

| | Palette | Head | Build | Cape |
|---|---|---|---|---|
| crimson | red / gold | helmet | normal | — |
| verdant | green / navy | bare | bulky | — |
| midnight | indigo / deep blue | cowl | normal | yes |
| frost | white / ice blue | helmet | slim | yes |
| ember | orange / rust | domino | normal | — |
| violet | purple / pink | domino | slim | yes |
| onyx | near-black / silver | cowl | bulky | yes |
| solar | gold / cream | visor | normal | yes |
| abyss | deep teal / aqua | helmet | slim | — |
| sable | charcoal / olive | hood | slim | — |
| bronze | copper / teal | hood | bulky | yes |
| vermeil | magenta / warm gold | visor | normal | yes |

**On licensed characters:** sprite sets for Marvel, DC and similar properties
are not available under any licence that permits bundling them here — what
circulates online is unlicensed fan art, and this repository is public and MIT,
so shipping it would hand the problem on to everyone who cloned it. The `hero`
pack exists to scratch that itch honestly. If you own a sprite pack you are
entitled to use, the widget will pick it up with no code change: drop the frames
into `app/assets/pets/<name>/` named `<colour>_<action>_8fps.gif`, add an
`icon.png`, and run `python app/tools/gen-metrics.py`.

## Type

None bundled. The widget uses Segoe UI Variable Display, already on the machine,
and loads no webfont — see `font-family` in `app/src/index.css`.
