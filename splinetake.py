#!/usr/bin/env python3
"""splinetake: download and decode spline (.splinecode) scenes to JSON.

spline (spline.design) publishes interactive 3d scenes as `.splinecode` files
served from prod.spline.design. the format is msgpackr (github.com/kriszyp/msgpackr)
with record structures plus six custom extension types. this file contains a
pure-python reimplementation of the msgpackr decoder (decode side only), with
the same semantics as the official @splinetool/viewer runtime:

  - record definitions: fixext1/2 with extension type 114 ('r')
  - record references: tokens 0x40-0x7f (single byte) or a second-byte reader
  - six spline extensions (types 1-6): uuid refs, typed blobs, pass-throughs
  - structured-clone extensions 0x69 (id) / 0x70 (pointer) for cycles
  - misc msgpackr extensions: bigint (0x42), date (0xff), typed arrays (0x74),
    set (0x73), regexp (0x78), error (0x65), bundled strings (0x62/0xc1)

output is plain JSON: the full scene graph (objects, materials, geometry,
physics, events, states, frames) exactly as the runtime consumes it.

usage:
  python3 splinetake.py <scene-url-or-file> [outdir]

no third-party dependencies. MIT licensed.
"""

import datetime
import json
import os
import struct
import sys
import urllib.request

C1 = {"$c1": True}  # msgpackr's "never used" marker

TYPED_ARRAY_FORMATS = {
    0: ("b", 1), 1: ("B", 1), 2: ("B", 1), 3: ("h", 2), 4: ("H", 2),
    5: ("i", 4), 6: ("I", 4), 7: ("f", 4), 8: ("d", 8), 9: ("q", 8), 10: ("Q", 8),
}


def as_safe_string(value):
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if value is None:
        return "null"
    raise ValueError("invalid property type for record: %r" % (value,))


class Unpackr:
    def __init__(self, data):
        self.src = data
        self.pos = 0
        self.end = len(data)
        self.structures = {}      # record id -> list of keys
        self.structure_meta = {}  # record id -> (first_byte, high_byte)
        self.reference_map = {}    # structured-clone id -> {"target","used"}

    # -- primitives --------------------------------------------------------

    def _u8(self):
        v = self.src[self.pos]
        self.pos += 1
        return v

    def _u16(self):
        v = struct.unpack_from(">H", self.src, self.pos)[0]
        self.pos += 2
        return v

    def _u32(self):
        v = struct.unpack_from(">I", self.src, self.pos)[0]
        self.pos += 4
        return v

    def _i8(self):
        v = struct.unpack_from(">b", self.src, self.pos)[0]
        self.pos += 1
        return v

    def _i16(self):
        v = struct.unpack_from(">h", self.src, self.pos)[0]
        self.pos += 2
        return v

    def _i32(self):
        v = struct.unpack_from(">i", self.src, self.pos)[0]
        self.pos += 4
        return v

    def _f32(self):
        v = struct.unpack_from(">f", self.src, self.pos)[0]
        self.pos += 4
        return v

    def _f64(self):
        v = struct.unpack_from(">d", self.src, self.pos)[0]
        self.pos += 8
        return v

    def _i64(self):
        v = struct.unpack_from(">q", self.src, self.pos)[0]
        self.pos += 8
        return v

    def _string(self, length):
        s = self.src[self.pos:self.pos + length]
        self.pos += length
        return s.decode("utf-8", errors="replace")

    # -- entry -------------------------------------------------------------

    def unpack(self):
        result = self.read()
        if self.pos != self.end:
            raise ValueError(
                "data read but end of buffer not reached at %d/%d" % (self.pos, self.end))
        return result

    # -- core read ---------------------------------------------------------

    def read(self):
        token = self._u8()
        if token < 0xA0:
            if token < 0x80:
                if token < 0x40:
                    return token
                structure = self.structures.get(token & 0x3F)
                if structure is not None:
                    return self.structure_read(structure)
                return token
            if token < 0x90:  # fixmap
                return self.read_map(token - 0x80)
            return [self.read() for _ in range(token - 0x90)]  # fixarray
        if token < 0xC0:  # fixstr
            return self._string(token - 0xA0)

        if token == 0xC0:
            return None
        if token == 0xC1:
            return C1
        if token == 0xC2:
            return False
        if token == 0xC3:
            return True
        if token == 0xC4:
            return self.read_bin(self._u8())
        if token == 0xC5:
            return self.read_bin(self._u16())
        if token == 0xC6:
            return self.read_bin(self._u32())
        if token == 0xC7:
            return self.read_ext(self._u8())
        if token == 0xC8:
            return self.read_ext(self._u16())
        if token == 0xC9:
            return self.read_ext(self._u32())
        if token == 0xCA:
            return self._f32()
        if token == 0xCB:
            return self._f64()
        if token == 0xCC:
            return self._u8()
        if token == 0xCD:
            return self._u16()
        if token == 0xCE:
            return self._u32()
        if token == 0xCF:
            return struct.unpack_from(">Q", self.src, self.pos)[0]
        if token == 0xD0:
            return self._i8()
        if token == 0xD1:
            return self._i16()
        if token == 0xD2:
            return self._i32()
        if token == 0xD3:
            return self._i64()
        if token == 0xD4:  # fixext1
            return self.fixext1(self._u8())
        if token == 0xD5:  # fixext2
            if self.src[self.pos] == 0x72:  # record definition (id + high byte)
                self.pos += 1
                return self.record_definition(self._u8() & 0x3F, self._u8())
            return self.read_ext(2)
        if token == 0xD6:
            return self.read_ext(4)
        if token == 0xD7:
            return self.read_ext(8)
        if token == 0xD8:
            return self.read_ext(16)
        if token == 0xD9:
            return self._string(self._u8())
        if token == 0xDA:
            return self._string(self._u16())
        if token == 0xDB:
            return self._string(self._u32())
        if token == 0xDC:
            return [self.read() for _ in range(self._u16())]
        if token == 0xDD:
            return [self.read() for _ in range(self._u32())]
        if token == 0xDE:
            return self.read_map(self._u16())
        if token == 0xDF:
            return self.read_map(self._u32())
        if token >= 0xE0:
            return token - 0x100
        raise ValueError("unknown msgpackr token 0x%02x at %d" % (token, self.pos - 1))

    def read_map(self, n):
        obj = {}
        for _ in range(n):
            key = self.read_key()
            if key == "__proto__":
                key = "__proto_"
            obj[key] = self.read()
        return obj

    def read_bin(self, length):
        data = self.src[self.pos:self.pos + length]
        self.pos += length
        return data

    def read_key(self):
        length = self.src[self.pos]
        if 0xA0 <= length < 0xC0:
            self.pos += 1
            return self._string(length - 0xA0)
        return as_safe_string(self.read())

    # -- records -----------------------------------------------------------

    def record_definition(self, record_id, high_byte=None):
        keys = self.read()
        if not isinstance(keys, list):
            raise ValueError("record definition keys must be an array")
        structure = [as_safe_string(k) for k in keys]
        first_byte = record_id
        if high_byte is not None:
            if record_id < 32:
                record_id = -((high_byte << 5) + record_id)
            else:
                record_id = (high_byte << 5) + record_id
        self.structures[record_id] = structure
        self.structure_meta[record_id] = (first_byte, high_byte)
        return self.read_structure_body(structure)

    def structure_read(self, structure):
        rid = self._id_of_structure(structure)
        first_byte, high = self.structure_meta.get(rid, (None, None))
        if high == 0:
            second = self._u8()  # second-byte reader
            if second == 0:
                return self.read_structure_body(structure)
            if first_byte < 32:
                target_id = -(first_byte + (second << 5))
            else:
                target_id = first_byte + (second << 5)
            target = self.structures.get(target_id)
            if target is None:
                raise ValueError("record id %d is not defined" % target_id)
            return self.structure_read(target)
        return self.read_structure_body(structure)

    def _id_of_structure(self, structure):
        for rid, s in self.structures.items():
            if s is structure:
                return rid
        return None

    def read_structure_body(self, structure):
        obj = {}
        for key in structure:
            if key == "__proto__":
                key = "__proto_"
            obj[key] = self.read()
        return obj

    # -- extensions --------------------------------------------------------

    def read_ext(self, length):
        ext_type = self._u8()
        start = self.pos
        self.pos += length
        data = self.src[start:self.pos]
        handler = EXTENSIONS.get(ext_type)
        if handler is None:
            raise ValueError("unknown extension type %d" % ext_type)
        return handler(self, data)

    def fixext1(self, ext_type):
        """msgpackr fixext1 dispatch, same three branches as the runtime:

        - 0x72 ('r'): record definition (id in the data byte)
        - class extensions (spline's types 1-6, registered with a .read):
          skip the filler byte, then read the payload from the stream
        - function extensions: 0 (notepack undefined) is noBuffer -> skip the
          data byte and return undefined. others receive the 1 data byte.
        """
        if ext_type == 0x72:
            return self.record_definition(self._u8() & 0x3F)
        handler = CLASS_EXTENSIONS.get(ext_type)
        if handler is not None:
            self.pos += 1  # skip filler byte, payload follows in the stream
            return handler(self.read())
        if ext_type == 0:  # noBuffer: undefined
            self.pos += 1
            return None
        fn = EXTENSIONS.get(ext_type)
        if fn is not None:
            data = self.src[self.pos:self.pos + 1]
            self.pos += 1
            return fn(self, data)
        raise ValueError("unknown extension %d" % ext_type)


# spline's six custom extension types (as registered by the official viewer).
# in the runtime they carry class identity; here they decode to plain data.
CLASS_EXTENSIONS = {
    1: lambda v: v,
    2: lambda v: v,
    3: lambda v: v,
    4: lambda v: {"id": v},
    5: lambda v: {"data": v},
    6: lambda v: v,
}


def _ext_bigint(u, data):
    head_length = len(data) % 8 or 8
    val = data[0] - 0x100 if data[0] & 0x80 else data[0]
    for i in range(1, head_length):
        val = (val << 8) | data[i]
    if len(data) != head_length:
        rest = int.from_bytes(data[head_length:], "big")
        val = (val << ((len(data) - head_length) * 8)) | rest
    return val


def _ext_date(u, data):
    if len(data) == 4:
        secs = struct.unpack(">I", data)[0]
        return datetime.datetime.fromtimestamp(secs, datetime.timezone.utc).isoformat()
    if len(data) == 8:
        ms = (((data[0] << 22) + (data[1] << 14) + (data[2] << 6) + (data[3] >> 2)) / 1e6
               + (((data[3] & 0x3) << 32) + struct.unpack(">I", data[4:])[0]) * 1000.0)
        return datetime.datetime.fromtimestamp(ms / 1000.0, datetime.timezone.utc).isoformat()
    if len(data) == 12:
        secs = struct.unpack(">i", data[:4])[0] / 1e6
        tail = int.from_bytes(data[4:], "big", signed=False)
        if data[4] & 0x80:
            tail -= 1 << 64
        return datetime.datetime.fromtimestamp(secs + tail, datetime.timezone.utc).isoformat()
    return None


def _ext_set(u, data):
    return {"$set": u.read()}


def _ext_regexp(u, data):
    return {"$regex": u.read()}


def _ext_error(u, data):
    return {"$error": u.read()}


def _ext_typed_array(u, data):
    type_code = data[0]
    raw = data[1:]
    if type_code == 16:
        return list(raw)
    if type_code in TYPED_ARRAY_FORMATS:
        f, size = TYPED_ARRAY_FORMATS[type_code]
        n = len(raw) // size
        return list(struct.unpack("<%d%s" % (n, f), raw[:n * size]))
    raise ValueError("could not decode typed array code %d" % type_code)


def _ext_id(u, data):
    """structured-clone id: reserve a placeholder, then read the target value."""
    rid = struct.unpack(">I", data)[0]
    entry = {"target": {}, "used": False}
    u.reference_map[rid] = entry
    properties = u.read()
    if not entry["used"]:
        # no cycle: hand back the real object AND update the entry so later
        # pointer(id) refs return the same object (msgpackr: refEntry.target = properties)
        entry["target"] = properties
        return properties
    if isinstance(properties, dict) and isinstance(entry["target"], dict):
        entry["target"].update(properties)
        return entry["target"]
    return properties


def _ext_pointer(u, data):
    rid = struct.unpack(">I", data)[0]
    entry = u.reference_map.get(rid)
    if entry is None:
        raise ValueError("pointer to unknown reference id %d" % rid)
    entry["used"] = True
    return entry["target"]


EXTENSIONS = {
    0: lambda u, data: None,
    0x42: _ext_bigint,
    0x62: lambda u, data: None,  # bundled strings (rare); body read unaffected
    0x65: _ext_error,
    0x69: _ext_id,
    0x70: _ext_pointer,
    0x73: _ext_set,
    0x74: _ext_typed_array,
    0x78: _ext_regexp,
    0xFF: _ext_date,
}


def decode_splinecode(data):
    return Unpackr(data).unpack()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def fetch(url, timeout=60):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (splinetake)",
        "Accept": "*/*",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def scene_summary(doc):
    types = {}

    def walk(node):
        data = node.get("data") if isinstance(node, dict) else None
        if isinstance(data, dict):
            t = data.get("type")
            if t:
                types[t] = types.get(t, 0) + 1
        for child in ((node or {}).get("children") or []):
            walk(child)

    scene = doc.get("scene") or {}
    for obj in scene.get("objects") or []:
        walk(obj)
    frames = doc.get("frames") or []
    n_frames = len(frames) if isinstance(frames, (list, dict)) else 0
    return {
        "schema": doc.get("schema"),
        "objects": sum(types.values()),
        "object_types": types,
        "frames": n_frames,
    }


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    target = argv[1]
    outdir = argv[2] if len(argv) > 2 else "."

    if target.startswith("http://") or target.startswith("https://"):
        data = fetch(target)
        parts = [p for p in target.rstrip("/").split("/") if p]
        name = parts[-2] if len(parts) >= 2 else "scene"
    else:
        with open(target, "rb") as f:
            data = f.read()
        name = os.path.splitext(os.path.basename(target))[0]

    if data[:3] != b"\xd4r@":
        raise SystemExit(
            "not a .splinecode file (magic %r); runtime exports start with d4 72 40"
            % data[:3])

    doc = decode_splinecode(data)

    os.makedirs(outdir, exist_ok=True)
    base = os.path.join(outdir, name)
    raw_path = base + ".splinecode"
    json_path = base + ".json"
    with open(raw_path, "wb") as f:
        f.write(data)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1, ensure_ascii=False)
        f.write("\n")

    s = scene_summary(doc)
    print("decoded %s" % name)
    print("  raw:      %s (%d bytes)" % (raw_path, len(data)))
    print("  json:     %s" % json_path)
    print("  schema:   %s" % s["schema"])
    print("  objects:  %d  %s" % (s["objects"], s["object_types"]))
    print("  frames:   %s" % s["frames"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
