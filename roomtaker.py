#!/usr/bin/env python3
"""
roomtaker — drop a rooms.xyz link, get your room as a .vox (+ the full project zip).

How it works (all public endpoints, no auth):
  1. rooms.xyz/<uname>/<roomId>  ->  Firestore doc rooms-catalog/<uname>__<roomId>
  2. the doc contains the owner's uid and the room's project zipName
  3. the whole project zip (room.dat scene tree + all .vox models + media) is
     public on their CDN: cdn.things-static.net/things-roomsxyz-projects-v2/...
  4. roomtaker parses room.dat, merges every model at its transform into a
     single whole-room .vox (MagicaVoxel v200), and packages everything.

Usage:
  python roomtaker.py <room_link> [more_links...] [-o OUTDIR]
  python roomtaker.py --user <uname> [--limit 50] [-o OUTDIR]

Output per room: <uname>__<roomid>/  containing room.vox, manifest.json,
models/*.vox (originals), room.dat, project.dat, and original.zip.

Notes / known limits (honest ones):
  - Rotations that aren't multiples of 90 deg are snapped to the nearest 90.
    (A .vox can't represent a 37-degree rotation.) Exact transforms are kept
    in manifest.json, and the untouched originals are in the zip.
  - Flipbook (multi-frame) models export their start frame (frame 0).
  - Each model is anchored bounding-box-center at its node position; the room
    shell (the base box in the editor) isn't a model, so it isn't included.
  - Scripts, sounds, physics and glow don't exist in the vox format — they
    remain in original.zip and manifest.json.

MIT licensed. Rooms(R) is a product of Things, Inc — this is an unofficial
fan tool for personal backups/remixes of rooms that Rooms makes remixable
by design.
"""

import argparse
import io
import json
import math
import os
import re
import struct
import sys
import time
import urllib.parse
import urllib.request
import zipfile

FIRESTORE_URL = ("https://firestore.googleapis.com/v1/projects/things-roomsxyz"
                 "/databases/(default)/documents/rooms-catalog/{doc}?key={key}")
RUNQUERY_URL = ("https://firestore.googleapis.com/v1/projects/things-roomsxyz"
                "/databases/(default)/documents:runQuery?key={key}")
CDN_ZIP_URL = ("https://cdn.things-static.net/things-roomsxyz-projects-v2"
               "/users/{uid}/rooms/{zipname}")

# Public web key used by rooms.xyz's own JS (obfuscated there with o2s(); the
# site ships this to every visitor, it only identifies the anonymous web app).
def _o2s_decode(s):
    return "".join(chr(int(p, 9) - 123) for p in s.split("9"))

WEB_KEY = _o2s_decode(
    "22892379302926492489301922892419243923192539283924092179256923592689236"
    "928192319232925492409236921292679277927392329275928392529235927292109271"
    "925192149246")

USER_AGENT = "roomtaker/1.0 (personal room backup tool)"


def http_get(url, retries=3, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last = None
    for i in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:
            last = e
            time.sleep(1 + i * 2)
    raise RuntimeError(f"GET failed for {url}: {last}")


def http_post_json(url, payload, retries=3, timeout=60):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={"User-Agent": USER_AGENT,
                                          "Content-Type": "application/json"})
    last = None
    for i in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            last = e
            time.sleep(1 + i * 2)
    raise RuntimeError(f"POST failed for {url}: {last}")


# --------------------------------------------------------------------------
# link parsing / catalog lookup
# --------------------------------------------------------------------------

def parse_room_link(link):
    """Accepts rooms.xyz/<uname>/<room>, <uname>.rooms.xyz/<room>, bare room ids."""
    link = link.strip()
    m = re.match(
        r"(?:https?://)?(?:([a-z0-9_-]+)\.)?rooms\.xyz/?([a-z0-9_-]+)?/?"
        r"([a-z0-9_-]+)?(?:[?#].*)?$", link, re.I)
    if not m:
        raise ValueError(f"not a rooms.xyz link: {link}")
    sub, a, b = m.groups()
    sub = sub.lower() if sub else None
    reserved = {"www", "create", "play", "docs", "help", "welcome", "terms",
                "privacy", "news", "jelloplex", "delete-account"}
    if sub and sub not in reserved:
        return sub, (a or "").lower()          # uname.rooms.xyz/room
    if a and b:
        return a.lower(), b.lower()            # rooms.xyz/uname/room
    if a:
        return None, a.lower()                 # rooms.xyz/room (needs --user)
    raise ValueError(f"can't parse room from link: {link}")


def catalog_get(uname, room_id):
    doc = f"{uname}__{room_id}"
    url = FIRESTORE_URL.format(doc=urllib.parse.quote(doc, safe=""), key=WEB_KEY)
    data = json.loads(http_get(url))
    if "fields" not in data:
        raise RuntimeError(f"no catalog record for {doc}")
    f = data["fields"]

    def g(name, default=""):
        v = f.get(name, {})
        for k in ("stringValue", "integerValue", "booleanValue"):
            if k in v:
                return v[k]
        return default

    info = {
        "uid": g("uid"),
        "uname": g("uname"),
        "roomId": g("roomId"),
        "zipName": g("zipName"),
        "visibility": g("visibility"),
        "likes": g("likes", "0"),
        "views": g("views", "0"),
        "numVoxFiles": g("r_infoNumVoxFiles", "0"),
        "numThings": g("r_infoNumThings", "0"),
        "created": g("created", ""),
        "modified": g("timestamp", ""),
    }
    if not info["uid"] or not info["zipName"]:
        raise RuntimeError(f"catalog record for {doc} lacks uid/zipName")
    return info


def catalog_user_rooms(uname, limit=50):
    q = {
        "structuredQuery": {
            "from": [{"collectionId": "rooms-catalog"}],
            "where": {
                "compositeFilter": {
                    "op": "AND",
                    "filters": [
                        {"fieldFilter": {"field": {"fieldPath": "uname"},
                                         "op": "EQUAL",
                                         "value": {"stringValue": uname}}},
                        {"fieldFilter": {"field": {"fieldPath": "visibility"},
                                         "op": "EQUAL",
                                         "value": {"stringValue": "public"}}},
                    ],
                }
            },
            "limit": int(limit),
        }
    }
    url = RUNQUERY_URL.format(key=WEB_KEY)
    results = http_post_json(url, q)
    rooms = []
    for r in results:
        doc = r.get("document") or {}
        flds = doc.get("fields", {})
        rid = flds.get("roomId", {}).get("stringValue")
        if rid:
            rooms.append(rid)
    return rooms


# --------------------------------------------------------------------------
# room.dat (protobuf text format) parsing
# --------------------------------------------------------------------------

class TextProtoParser:
    """Minimal parser for the protobuf text format used by room.dat."""

    def __init__(self, text):
        self.s = text
        self.i = 0

    def _skip_ws(self):
        while self.i < len(self.s):
            c = self.s[self.i]
            if c in " \t\r\n":
                self.i += 1
            elif c == "#":                      # comment
                while self.i < len(self.s) and self.s[self.i] != "\n":
                    self.i += 1
            else:
                return

    def _read_quoted(self):
        self.i += 1
        out = []
        while self.i < len(self.s):
            c = self.s[self.i]
            if c == "\\":
                nxt = self.s[self.i + 1]
                if nxt == "u":
                    hexcode = self.s[self.i + 2:self.i + 6]
                    out.append(chr(int(hexcode, 16)))
                    self.i += 6
                elif nxt == "n":
                    out.append("\n"); self.i += 2
                elif nxt == "t":
                    out.append("\t"); self.i += 2
                elif nxt == "r":
                    out.append("\r"); self.i += 2
                elif nxt == '"':
                    out.append('"'); self.i += 2
                elif nxt == "\\":
                    out.append("\\"); self.i += 2
                else:
                    out.append(nxt); self.i += 2
            elif c == '"':
                self.i += 1
                return "".join(out)
            else:
                out.append(c)
                self.i += 1
        raise ValueError("unterminated string")

    def _read_bare(self):
        start = self.i
        while self.i < len(self.s) and self.s[self.i] not in " \t\r\n:{}":
            self.i += 1
        return self.s[start:self.i]

    def read_value(self):
        self._skip_ws()
        c = self.s[self.i]
        if c == '"':
            return self._read_quoted()
        if c == "{":
            # could be a single message (renderSpec: {...}) or a list of
            # messages (children: { {node} {node} }); peek past the brace.
            self.i += 1
            self._skip_ws()
            if self.i < len(self.s) and self.s[self.i] == "{":
                items = []
                while True:
                    self._skip_ws()
                    assert self.s[self.i] == "{", f"expected node at {self.i}"
                    items.append(self.read_message())
                    self._skip_ws()
                    if self.s[self.i] == "}":
                        self.i += 1
                        return items
            return self._message_body()
        return self._read_bare()

    def read_message(self):
        # at '{'
        self.i += 1
        return self._message_body()

    def read_document(self):
        # top-level: room.dat has no enclosing braces; ends at EOF
        fields = []
        while True:
            self._skip_ws()
            if self.i >= len(self.s):
                return fields
            if self.s[self.i] == "}":
                self.i += 1
                return fields
            name = self._read_bare()
            self._skip_ws()
            if self.i < len(self.s) and self.s[self.i] == ":":
                self.i += 1
                self._skip_ws()
                fields.append((name, self.read_value()))
            elif self.i < len(self.s) and self.s[self.i] == "{":
                fields.append((name, self.read_value()))
        return fields

    def _message_body(self):
        # positioned just past the opening '{'
        fields = []
        while True:
            self._skip_ws()
            if self.i >= len(self.s):
                raise ValueError("unterminated message")
            if self.s[self.i] == "}":
                self.i += 1
                return fields
            name = self._read_bare()
            self._skip_ws()
            if self.s[self.i] == ":":
                self.i += 1
                self._skip_ws()
                fields.append((name, self.read_value()))
            elif self.s[self.i] == "{":
                # repeated block without colon: children { {node} ... }
                fields.append((name, self.read_value()))


def msg_to_dict(fields):
    d = {}
    for k, v in fields:
        if k in d:
            if not isinstance(d[k], list):
                d[k] = [d[k]]
            d[k].append(v)
        else:
            d[k] = v
    return d


def vec3(s, default="0, 0, 0"):
    if not s:
        s = default
    parts = [p.strip() for p in str(s).split(",")]
    while len(parts) < 3:
        parts.append("0")
    return tuple(float(_num(p)) for p in parts[:3])


def _num(p):
    p = p.strip()
    if p.startswith("0x"):
        return int(p, 16)
    try:
        return int(p)
    except ValueError:
        return float(p)


# --------------------------------------------------------------------------
# vox (MagicaVoxel v200) reading / writing
# --------------------------------------------------------------------------

def _read_dict(buf, off):
    (n,) = struct.unpack_from("<I", buf, off)
    off += 4
    d = {}
    for _ in range(n):
        (klen,) = struct.unpack_from("<I", buf, off); off += 4
        k = buf[off:off + klen].decode("utf-8", "replace"); off += klen
        (vlen,) = struct.unpack_from("<I", buf, off); off += 4
        v = buf[off:off + vlen].decode("utf-8", "replace"); off += vlen
        d[k] = v
    return d, off


def read_vox(data):
    """Return {'frames': [...]}. 'frames' are models in file order; for
    scene-graph files (nTRN/nGRP/nSHP) the node placements are returned too.
    Handles flat multi-frame layouts (Rooms flipbooks) and standard files."""
    if data[:4] != b"VOX ":
        raise ValueError("not a MagicaVoxel .vox file")
    cs, chs = struct.unpack("<II", data[12:20])
    off = 20 + cs
    end = off + chs
    palette = None
    frames = []
    size = None
    nodes = {}      # nodeId -> ('TRN', child, frames) | ('GRP', [children]) | ('SHP', [(model_idx,...)])
    while off < end:
        cid = data[off:off + 4].decode("ascii", "replace")
        content_len, children_len = struct.unpack("<II", data[off + 4:off + 12])
        body = off + 12
        b = data; o = body
        if cid == "RGBA":
            palette = [tuple(data[body + i * 4: body + i * 4 + 4])
                       for i in range(256)]
        elif cid == "SIZE":
            size = struct.unpack("<III", data[body:body + 12])
        elif cid == "XYZI":
            (n,) = struct.unpack("<I", data[body:body + 4])
            voxels = [tuple(data[body + 4 + i * 4: body + 8 + i * 4])
                      for i in range(n)]
            if size is not None:
                frames.append({"size": size, "voxels": voxels})
            size = None
        elif cid == "nTRN":
            (nid,) = struct.unpack_from("<I", b, o); o += 4
            attrs, o = _read_dict(b, o)
            (child, reserved, layer, nframes) = struct.unpack_from("<iiii", b, o); o += 16
            fdicts = []
            for _ in range(nframes):
                fd, o = _read_dict(b, o)
                fdicts.append(fd)
            nodes[nid] = ("TRN", attrs, child, fdicts)
        elif cid == "nGRP":
            (nid,) = struct.unpack_from("<I", b, o); o += 4
            attrs, o = _read_dict(b, o)
            (nch,) = struct.unpack_from("<I", b, o); o += 4
            children = list(struct.unpack_from("<%di" % nch, b, o)) if nch else []
            nodes[nid] = ("GRP", attrs, children)
        elif cid == "nSHP":
            (nid,) = struct.unpack_from("<I", b, o); o += 4
            attrs, o = _read_dict(b, o)
            (nm,) = struct.unpack_from("<I", b, o); o += 4
            models = []
            for _ in range(nm):
                (mid,) = struct.unpack_from("<I", b, o); o += 4
                mattrs, o = _read_dict(b, o)
                models.append(mid)
            nodes[nid] = ("SHP", attrs, models)
        off = body + content_len + children_len

    if palette is None:
        palette = [(i, i, i, 255) for i in range(256)]

    out_frames = []
    for fr in frames:
        cols = []
        for (x, y, z, ci) in fr["voxels"]:
            if ci == 0:
                ci = 1
            cols.append((x, y, z, palette[ci - 1]))
        out_frames.append({"size": fr["size"], "voxels": cols})
    if not out_frames:
        out_frames = [{"size": (1, 1, 1), "voxels": []}]

    result = {"frames": out_frames}

    # if there is a scene graph, bake absolute placements of each model
    placements = []
    def place(nid, base_t, base_r):
        n = nodes.get(nid)
        if n is None:
            return
        kind = n[0]
        if kind == "TRN":
            child, fdicts = n[2], n[3]
            fr0 = fdicts[0] if fdicts else {}
            t = tuple(int(v) for v in (fr0.get("_t", "0 0 0")).split())
            place(child, (base_t[0] + t[0], base_t[1] + t[1], base_t[2] + t[2]),
                  base_r)
        elif kind == "GRP":
            for c in n[2]:
                place(c, base_t, base_r)
        elif kind == "SHP":
            for midx in n[2]:
                if 0 <= midx < len(out_frames):
                    placements.append((midx, base_t, base_r))
    roots = [nid for nid, n in nodes.items() if n[0] == "TRN"]
    # root = transform node that is nobody's child
    children = set()
    for nid, n in nodes.items():
        if n[0] == "TRN":
            children.add(n[2])
        elif n[0] == "GRP":
            children.update(n[2])
    roots = [nid for nid in roots if nid not in children]
    for r in roots:
        place(r, (0, 0, 0), None)
    if placements:
        merged = []
        for (midx, (tx, ty, tz), _r) in placements:
            for (x, y, z, c) in out_frames[midx]["voxels"]:
                merged.append((x + tx, y + ty, z + tz, c))
        if merged:
            result["merged"] = merged
    return result


def _dict_chunk(items):
    """MagicaVoxel DICT: count + (len-prefixed key, len-prefixed value) pairs."""
    body = struct.pack("<I", len(items))
    for k, v in items:
        kb = k.encode(); vb = str(v).encode()
        body += struct.pack("<I", len(kb)) + kb + struct.pack("<I", len(vb)) + vb
    return body


def _chunk(cid, content):
    return cid.encode() + struct.pack("<II", len(content), 0) + content


def write_vox(voxels, out_path, max_dim=256):
    """voxels: list of (x, y, z, (r,g,b,a)) in output integer coords.
    Scenes bigger than max_dim on any axis are split into several models
    placed with nTRN transforms (standard MagicaVoxel scene-graph format)."""
    if not voxels:
        raise ValueError("no voxels to write")
    xs = [v[0] for v in voxels]; ys = [v[1] for v in voxels]; zs = [v[2] for v in voxels]
    minx, miny, minz = min(xs), min(ys), min(zs)
    sx = max(xs) - minx + 1; sy = max(ys) - miny + 1; sz = max(zs) - minz + 1

    idx_of = {}
    for (_, _, _, c) in voxels:
        if c not in idx_of:
            idx_of[c] = len(idx_of) + 1
    if len(idx_of) > 255:      # quantize if needed
        idx_of = {}
        for (_, _, _, (r, g, b, a)) in voxels:
            c = (r & 0xF8, g & 0xF8, b & 0xF8, a)
            if c not in idx_of:
                idx_of[c] = len(idx_of) + 1
    pal_by_index = [None] * 256
    for c, i in idx_of.items():
        pal_by_index[i - 1] = c

    # ---- tiles ----
    tiles = []
    by_tile = {}
    for (x, y, z, c) in voxels:
        gx, gy, gz = x - minx, y - miny, z - minz
        key = (gx // max_dim, gy // max_dim, gz // max_dim)
        by_tile.setdefault(key, []).append((gx, gy, gz, c))
    for key in sorted(by_tile):
        tv = by_tile[key]
        ox, oy, oz = key[0] * max_dim, key[1] * max_dim, key[2] * max_dim
        txs = [v[0] for v in tv]; tys = [v[1] for v in tv]; tzs = [v[2] for v in tv]
        tiles.append({
            "origin": (ox, oy, oz),
            "size": (max(txs) - min(txs) + 1, max(tys) - min(tys) + 1,
                     max(tzs) - min(tzs) + 1),
            "voxels": [(x - min(txs), y - min(tys), z - min(tzs), c)
                       for (x, y, z, c) in tv],
        })

    models = bytearray()
    trns = bytearray()
    shape_ids = []
    # node ids: 0 = root transform, 1 = root group, then trn/shp pairs
    next_id = 2
    grp_children = []
    for tile in tiles:
        trn_id = next_id; shp_id = next_id + 1
        next_id += 2
        ox, oy, oz = tile["origin"]
        trn = struct.pack("<i", trn_id)
        trn += _dict_chunk([])
        trn += struct.pack("<iiii", shp_id, -1, 0, 1)
        trn += _dict_chunk([("_t", f"{ox} {oy} {oz}")])
        models += _chunk("nTRN", trn)
        shp = struct.pack("<i", shp_id)
        shp += _dict_chunk([])
        shp += struct.pack("<I", 1)
        model_index = len(shape_ids)
        shp += struct.pack("<i", model_index) + _dict_chunk([])
        models += _chunk("nSHP", shp)
        grp_children.append(trn_id)
        shape_ids.append(model_index)

        (tx, ty, tz) = tile["size"]
        models += _chunk("SIZE", struct.pack("<III", tx, ty, tz))
        xyzi = bytearray(struct.pack("<I", len(tile["voxels"])))
        for (x, y, z, c) in tile["voxels"]:
            xyzi += bytes((x, y, z, idx_of[c]))
        models += _chunk("XYZI", bytes(xyzi))

    # root transform -> root group
    root_trn = struct.pack("<i", 0) + _dict_chunk([("_name", "root")])
    root_trn += struct.pack("<iiii", 1, -1, 0, 1) + _dict_chunk([])
    root_grp = struct.pack("<i", 1) + _dict_chunk([])
    root_grp += struct.pack("<I", len(grp_children))
    root_grp += struct.pack("<%di" % len(grp_children), *grp_children)

    # global palette
    rgba = bytearray()
    for i in range(256):
        rgba += bytes(pal_by_index[i] or (0, 0, 0, 255))

    single = len(tiles) == 1
    body = bytearray()
    if not single:
        body += _chunk("nTRN", root_trn)
        body += _chunk("nGRP", root_grp)
    body += models
    body += _chunk("RGBA", bytes(rgba))

    out = (b"VOX " + struct.pack("<I", 200) + b"MAIN"
           + struct.pack("<II", 0, len(body)) + bytes(body))
    with open(out_path, "wb") as fh:
        fh.write(out)
    return {"size": (sx, sy, sz), "colors": len(idx_of),
            "voxels": len(voxels), "models": len(tiles)}


# --------------------------------------------------------------------------
# transforms / merge
# --------------------------------------------------------------------------

def _snap90(deg):
    return int(round(deg / 90.0)) % 4


def _rot_matrix(rx, ry, rz):
    """Unity-style euler (deg): R = Ry @ Rx @ Rz, snapped to quarter turns."""
    def rot_axis(axis, steps):
        m = [[1 if i == j else 0 for j in range(3)] for i in range(3)]
        j, k = (axis + 1) % 3, (axis + 2) % 3
        for _ in range(steps % 4):
            nj = [m[t][j] for t in range(3)]
            nk = [m[t][k] for t in range(3)]
            for t in range(3):
                m[t][j] = nk[t]
                m[t][k] = -nj[t]
        return m

    mz = rot_axis(2, _snap90(rz))
    mx = rot_axis(0, _snap90(rx))
    my = rot_axis(1, _snap90(ry))
    def mul(a, b):
        return tuple(tuple(sum(a[i][t] * b[t][j] for t in range(3))
                           for j in range(3)) for i in range(3))
    return mul(my, mul(mx, mz))


def _mat_vec(m, v):
    return (m[0][0] * v[0] + m[0][1] * v[1] + m[0][2] * v[2],
            m[1][0] * v[0] + m[1][1] * v[1] + m[1][2] * v[2],
            m[2][0] * v[0] + m[2][1] * v[1] + m[2][2] * v[2])


def hex_tint(h):
    h = (h or "#ffffff").lstrip("#")
    if len(h) == 8:
        h = h[:6]
    if len(h) != 6:
        return None
    try:
        r = int(h[0:2], 16); g = int(h[2:4], 16); b = int(h[4:6], 16)
    except ValueError:
        return None
    if (r, g, b) == (255, 255, 255):
        return None
    return (r / 255.0, g / 255.0, b / 255.0)


def merge_room(room_dat_text, vox_files, verbose=True):
    """Returns (scene_voxels, manifest_things, warnings)."""
    def _norm(v):
        # convert nested protobuf-text values: a list of (name, value) tuples
        # is a message; a list of messages is a repeated field (children).
        if (isinstance(v, list) and v and all(
                isinstance(x, tuple) and len(x) == 2 and isinstance(x[0], str)
                for x in v)):
            return {k: _norm(val) for k, val in v}
        if isinstance(v, list):
            return [_norm(x) for x in v]
        return v

    parser = TextProtoParser(room_dat_text)
    doc = msg_to_dict(parser.read_document())
    root = _norm(doc.get("root")) or {}
    warnings = []
    things = []

    scene = []
    stats = {"models": 0, "placed": 0, "missing": 0}

    def walk(node, parent_R, parent_S, parent_T, path):
        R_local = _rot_matrix(*vec3(node.get("localRot", "0, 0, 0")))
        S_local = float(node.get("scale", 1) or 1)
        T_local = vec3(node.get("localPos", "0, 0, 0"))
        R = tuple(tuple(sum(parent_R[i][t] * R_local[t][j] for t in range(3))
                        for j in range(3)) for i in range(3))
        S = parent_S * S_local
        T = tuple(parent_T[i] + _mat_vec(parent_R,
                tuple(c * parent_S for c in T_local))[i] for i in range(3))

        model = node.get("modelName", "")
        name = node.get("name", "")
        if model and model in vox_files:
            stats["models"] += 1
            vox = vox_files[model]
            frame_idx = 0
            anim = node.get("animSpec") or {}
            try:
                frame_idx = max(0, int(anim.get("startFrame", 0) or 0))
            except Exception:
                frame_idx = 0
            if frame_idx >= len(vox["frames"]):
                frame_idx = 0
            fr = vox["frames"][frame_idx]
            (vx, vy, vz) = fr["size"]           # MagicaVoxel axes: y up
            # Rooms maps vox(x,y,z) -> room(x,z,y): swap y/z
            scene_dims = (vx, vz, vy)
            tint = hex_tint(node.get("color"))
            placed_count = 0
            for (mx, my, mz, (r, g, b, a)) in fr["voxels"]:
                # center-anchored offset in room space
                p = (mx + 0.5 - vx / 2.0,      # x
                     mz + 0.5 - vz / 2.0,      # room y  <- vox z
                     my + 0.5 - vy / 2.0)      # room z  <- vox y
                pr = _mat_vec(R, tuple(c * S for c in p))
                wx, wy, wz = (T[0] + pr[0], T[1] + pr[1], T[2] + pr[2])
                if tint:
                    r = min(255, int(r * tint[0]))
                    g = min(255, int(g * tint[1]))
                    b = min(255, int(b * tint[2]))
                scene.append((wx, wy, wz, (r, g, b, a)))
                placed_count += 1
            stats["placed"] += 1
            things.append({
                "name": name, "model": model, "frame": frame_idx,
                "pos": list(T),
                "rot_input": list(vec3(node.get("localRot"))),
                "world_rot_snapped": [list(row) for row in R],
                "scale": S, "voxels": placed_count, "path": path,
            })
            rot = vec3(node.get("localRot"))
            if any(abs(((a % 90) + 90) % 90) > 0.01 for a in rot):
                warnings.append(
                    f"{name or model}: rotation {rot} not a multiple of 90°, snapped")
        elif model and model not in vox_files:
            stats["missing"] += 1
            warnings.append(f"{name or model}: model file {model} missing from project")

        for child in (node.get("children") or []):
            walk(child, R, S, T, path + [name or child.get("name", "?")])

    walk(root, ((1, 0, 0), (0, 1, 0), (0, 0, 1)), 1.0, (0.0, 0.0, 0.0), [])

    grid = {}
    for (x, y, z, c) in scene:
        grid[(int(math.floor(x)), int(math.floor(y)), int(math.floor(z)))] = c
    final = [(x, y, z, c) for (x, y, z), c in grid.items()]
    if verbose:
        print(f"  merged {stats['placed']}/{stats['models']} models, "
              f"{len(final)} voxels, {stats['missing']} missing")
    return final, things, warnings


# --------------------------------------------------------------------------
# per-room export
# --------------------------------------------------------------------------

def export_room(uname, room_id, outdir, keep_zip=True, verbose=True):
    info = catalog_get(uname, room_id)
    if verbose:
        print(f"[{uname}/{room_id}] {info['numVoxFiles']} vox files, "
              f"{info['numThings']} things, {info['likes']} likes")
    zip_bytes = http_get(CDN_ZIP_URL.format(uid=info["uid"], zipname=info["zipName"]))
    zf = zipfile.ZipFile(io.BytesIO(zip_bytes))

    vox_files = {}
    for name in zf.namelist():
        if name.endswith(".vox"):
            vox_files[os.path.basename(name)] = read_vox(zf.read(name))

    room_dat = zf.read("room.dat").decode("utf-8", "replace")
    try:
        project_dat = zf.read("project.dat").decode("utf-8", "replace")
    except KeyError:
        project_dat = ""

    scene, things, warnings = merge_room(room_dat, vox_files, verbose=verbose)

    room_dir = os.path.join(outdir, f"{uname}__{room_id}")
    os.makedirs(os.path.join(room_dir, "models"), exist_ok=True)

    # write originals
    for name in zf.namelist():
        if name.endswith(".vox"):
            with open(os.path.join(room_dir, "models", os.path.basename(name)), "wb") as fh:
                fh.write(zf.read(name))
    with open(os.path.join(room_dir, "room.dat"), "w") as fh:
        fh.write(room_dat)
    if project_dat:
        with open(os.path.join(room_dir, "project.dat"), "w") as fh:
            fh.write(project_dat)
    if keep_zip:
        with open(os.path.join(room_dir, "original.zip"), "wb") as fh:
            fh.write(zip_bytes)

    merged_stats = write_vox(scene, os.path.join(room_dir, "room.vox"))

    manifest = {
        "link": f"https://rooms.xyz/{uname}/{room_id}",
        "room": info,
        "merged": merged_stats,
        "things": things,
        "warnings": warnings,
        "notes": [
            "room.vox anchors each model's bounding-box center at its node position",
            "non-90-degree rotations are snapped to 90; exact values in 'things'",
            "flipbook models export their animSpec.startFrame (default frame 0)",
            "the untouched project is in original.zip (room.dat + models + media)",
        ],
    }
    with open(os.path.join(room_dir, "manifest.json"), "w") as fh:
        json.dump(manifest, fh, indent=2)
    if verbose:
        extra = f", {merged_stats['models']} models" if merged_stats.get("models", 1) > 1 else ""
        print(f"  -> {room_dir}/  room.vox {merged_stats['size']} "
              f"({merged_stats['voxels']} voxels, {merged_stats['colors']} colors{extra})")
    return room_dir, warnings


def main():
    ap = argparse.ArgumentParser(description="rooms.xyz -> whole-room .vox exporter")
    ap.add_argument("links", nargs="*", help="room links (or bare room ids with --user)")
    ap.add_argument("--user", help="username to export rooms from (or to resolve bare links)")
    ap.add_argument("--limit", type=int, default=50, help="max rooms when exporting a user")
    ap.add_argument("-o", "--outdir", default="roomtaker-out", help="output directory")
    args = ap.parse_args()

    jobs = []
    for link in args.links:
        uname, room_id = parse_room_link(link)
        if uname is None:
            if not args.user:
                sys.exit(f"link '{link}' has no username; pass --user or a full link")
            uname = args.user
        jobs.append((uname, room_id))

    if not jobs and args.user:
        rooms = catalog_user_rooms(args.user, args.limit)
        if not rooms:
            sys.exit(f"no public rooms found for user {args.user}")
        print(f"found {len(rooms)} public rooms for {args.user}")
        jobs = [(args.user, r) for r in rooms]

    if not jobs:
        ap.print_help()
        return

    failed = []
    for (uname, room_id) in jobs:
        try:
            export_room(uname, room_id, args.outdir)
        except Exception as e:
            print(f"[{uname}/{room_id}] FAILED: {e}")
            failed.append((uname, room_id))
        time.sleep(1.0)   # be polite
    if failed:
        print(f"\n{len(failed)} room(s) failed: {failed}")
        sys.exit(1)
    print("\ndone.")


if __name__ == "__main__":
    main()
