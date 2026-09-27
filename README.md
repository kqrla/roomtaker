# splinetake

take any published [spline](https://spline.design) scene home: download the
`.splinecode` runtime export from its public url and decode it to plain json.

    python3 splinetake.py <scene-url-or-file> [outdir]

examples:

    python3 splinetake.py https://prod.spline.design/9951u9cumiw2Ehj8/scene.splinecode out/
    python3 splinetake.py scene.splinecode out/

for every scene you get:

- `<name>.splinecode` — the original runtime file, byte for byte
- `<name>.json` — the fully decoded scene document

no dependencies, no account, no api key. pure python 3 stdlib.

## what a .splinecode file is

spline's export panel produces `.splinecode` files for the
`@splinetool/viewer` web component. served from `prod.spline.design/<id>/`,
they are public: any browser can fetch them, so this is the same data the
published viewer already gives everyone.

the format is [msgpackr](https://github.com/kriszyp/msgpackr) (messagepack
plus record structures) with six custom extension types. this repo contains
a decode-only port of the msgpackr reader, written against the actual
`@splinetool/viewer` bundle, including:

- record definitions (fixext 114 'r') and one/two-byte record references
- spline's six class extensions (uuid refs, blob data, pass-throughs)
- notepack-style undefined (ext 0), structured-clone cycles (ext 0x69/0x70),
  bigint, date, typed-array, set and regexp extensions

the port is verified byte-exact against the real msgpackr runtime on both the
official example scene (schema 30) and current published scenes (schema 115):
every value, key, array length and nesting level matches.

## the decoded json

top level: `schema`, `scene`, `frames`, `shared`, and on newer files
`version` (the runtime version that wrote the file).

- `scene.objects` — the object tree. each node has `fi` (z-order), `id`,
  `children` and `data`, and `data` holds the type-specific payload:
  transform, geometry, materials (with layers), physics, events, states.
- `scene.publish` — publish/render settings, camera controls, orbit config.
- `frames` — animation frames.
- `shared` — fonts, colors, sounds, particles, variables referenced by objects.

fields the runtime stores as `undefined` appear as `null` here (json has no
undefined; the runtime's own json dumps drop them instead).

## scope and intent

same spirit as [roomtaker](https://github.com/kqrla/roomtaker): public
published data, exportable by the very viewer that renders it, for backup and
interoperability. this is for scenes you published or have the right to take.
it does not log in, does not bypass any paywall, and cannot read anything a
plain browser cannot fetch.

MIT license. spline is a product of Spline, inc.; this project is independent
and not affiliated.
