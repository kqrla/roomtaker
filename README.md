# roomtaker

Drop a [rooms.xyz](https://rooms.xyz) link, get the whole room back as a MagicaVoxel `.vox` file.

```
python roomtaker.py https://rooms.xyz/btco/adventure -o out/
python roomtaker.py --user btco --limit 50 -o out/    # every public room by a user
```

No login, no API key, one file, Python stdlib only. `out/<user>__<room>/` ends up with:

- **`room.vox`** — the entire room merged into one MagicaVoxel file (v200). Every thing placed at its position, rotation and tint, in a 1:1 voxel scale.
- **`models/*.vox`** — the room's original model files, untouched.
- **`original.zip`, `room.dat`, `project.dat`** — the project exactly as the editor stores it.
- **`manifest.json`** — every thing: name, model, exact transform, tint, physics, glow, and its Lua script.

## How it works

All of rooms.xyz's data for a room is public — the site itself downloads it to render the room for any visitor:

1. `rooms.xyz/<uname>/<roomId>` maps to a Firestore document (`rooms-catalog` collection) that holds the owner uid and project zip name.
2. The project zip — the scene tree (`room.dat`), all `.vox` models, media — lives on their public CDN.
3. roomtaker parses `room.dat` (protobuf text format), walks the node tree, and composes each model at its local position/rotation/scale into one voxel scene, then writes a standard `.vox`.

If the scene is bigger than 256 voxels on any axis (the vox per-model limit), the output uses MagicaVoxel's own scene-graph format (`nTRN`/`nSHP` tiles), so the file still opens as one piece.

## Honest limits

- Angles that aren't multiples of 90° don't exist in the vox format — they're snapped to the nearest 90. Exact transforms stay in `manifest.json`.
- Flipbook (animated) models export their first frame.
- The room shell (the editor's base floor/walls) isn't model geometry, so it isn't synthesized.
- Scripts, sounds, physics types and glow have no vox equivalent — they're preserved in `manifest.json` / `original.zip`.
- Everything (position/rotation/tint) is exported; only *geometry* merges into `room.vox`.

## Why this is fine to use

Rooms encourages remixing: every room has "duplicate", models can be individually exported/imported as `.vox` from the editor, and the project data is served publicly to anyone who opens the room. roomtaker is the same operation, batched — an unofficial personal backup tool. Rooms® is a product of Things, Inc; this project isn't affiliated with them.

## License

MIT — see LICENSE.
