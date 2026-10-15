"""Build eqoa_server/reference_data/spell_effects.json from the disc's DATA2/SPELLFX.CSF.

SPELLFX.CSF is a compressed ESF file (CESF: zlib blocks, as in joukop/ESF-file-format's
CSFFile). It holds the client's spell effects: 238 SpellEffect objects (0xC200, the id in
their 0xC210 header) built from 332 ParticleDefinition objects (0xC000, id in 0xC010). The
three effect ids the Sandstorm server was seen sending (Return Home 0x8FB28233, Kick
0x0A286E2E, a mob's attack 0xDA9BEA11) are among them.

Nothing names the effects or says which spell used which; the original server kept that.
To guess, each effect gets a colour: the average of its particles' colour ramps (RGBA float
quads from byte 0x6C of the particle body; Return Home's particles come out blue, which is
its glow, and Kick's orange). Only ids, particle counts and colours are written, not the
disc's data.

    python3 tools/import_spell_effects.py /path/to/DATA2/SPELLFX.CSF
"""
from __future__ import annotations

import colorsys
import json
import struct
import sys
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "eqoa_server" / "reference_data" / "spell_effects.json"
SPELL_EFFECT, SPELL_EFFECT_HEADER, SPELL_EFFECT_BODY = 0xC200, 0xC210, 0xC220
PARTICLE, PARTICLE_HEADER, PARTICLE_BODY = 0xC000, 0xC010, 0xC020
RAMP_START = 0x6C


def decompress_csf(data: bytes) -> bytes:
    blocks = struct.unpack_from("<I", data, 4)[0]
    offset = struct.unpack_from("<Q", data, 24)[0]
    out = bytearray()
    for _ in range(blocks):
        size = struct.unpack_from("<Q", data, offset)[0] & 0xFFFFFFFF
        out += zlib.decompress(data[offset + 8 : offset + 8 + size])
        offset += 8 + size
    return bytes(out)


def objects(esf: bytes) -> list[dict]:
    """Every object: type, body offset and size, children (ESF layout: u16 type, u16
    version, u32 size, u32 child count, then the children inside the body)."""
    found: list[dict] = []

    def read(pos: int) -> tuple[dict, int]:
        kind, _version, size, count = struct.unpack_from("<HHII", esf, pos)
        obj = {"type": kind, "off": pos + 12, "size": size, "kids": []}
        found.append(obj)
        child = pos + 12
        for _ in range(count):
            kid, child = read(child)
            obj["kids"].append(kid)
        return obj, pos + 12 + size

    pos = struct.unpack_from("<Q", esf, 16)[0]
    while pos + 12 <= len(esf):
        size = struct.unpack_from("<I", esf, pos + 4)[0]
        if not size or pos + 12 + size > len(esf):
            break
        _, pos = read(pos)
    return found


def _part(obj: dict, kind: int) -> dict:
    return next(kid for kid in obj["kids"] if kid["type"] == kind)


def ramp_colour(body: bytes) -> tuple[float, float, float] | None:
    quads = []
    for at in range(RAMP_START, len(body) - 15, 16):
        quad = struct.unpack_from("<4f", body, at)
        if not all(0.0 <= value <= 1.0001 for value in quad):
            break
        quads.append(quad)
    if not quads:
        return None
    return tuple(sum(quad[i] for quad in quads) / len(quads) for i in range(3))


def effects(esf: bytes) -> list[dict]:
    objs = objects(esf)
    particles = {}
    for obj in objs:
        if obj["type"] == PARTICLE:
            pid = struct.unpack_from("<I", esf, _part(obj, PARTICLE_HEADER)["off"])[0]
            body = _part(obj, PARTICLE_BODY)
            particles[pid] = ramp_colour(esf[body["off"] : body["off"] + body["size"]])
    out = []
    for obj in objs:
        if obj["type"] != SPELL_EFFECT:
            continue
        eid = struct.unpack_from("<I", esf, _part(obj, SPELL_EFFECT_HEADER)["off"])[0]
        body = _part(obj, SPELL_EFFECT_BODY)
        raw = esf[body["off"] : body["off"] + body["size"]]
        used = sorted({struct.unpack_from("<I", raw, i)[0] for i in range(len(raw) - 3)} & particles.keys())
        colours = [particles[p] for p in used if particles[p] is not None]
        rgb = tuple(round(sum(c[i] for c in colours) / len(colours), 3) for i in range(3)) if colours else None
        out.append({"id": eid, "particles": len(used), "rgb": rgb, "colour": main_colour(colours)})
    return out


def main_colour(colours: list[tuple[float, float, float]]) -> str:
    """The colour most of the effect's particles have, counting white and grey only when
    nothing else shows (Return Home: three blue particles, a red, a yellow and a grey)."""
    names = [colour_name(rgb) for rgb in colours]
    vivid = [name for name in names if name not in ("white", "grey")] or names
    return max(sorted(set(vivid)), key=vivid.count) if vivid else "none"


def colour_name(rgb) -> str:
    if rgb is None:
        return "none"
    hue, light, sat = colorsys.rgb_to_hls(*rgb)
    if sat < 0.2 or light > 0.85:
        return "white" if light > 0.55 else "grey"
    hue *= 360
    for limit, name in ((20, "red"), (45, "orange"), (70, "yellow"), (160, "green"), (200, "cyan"),
                        (255, "blue"), (300, "purple"), (340, "pink"), (361, "red")):
        if hue < limit:
            return name
    return "red"


def main(path: str) -> None:
    found = effects(decompress_csf(Path(path).read_bytes()))
    OUT.write_text(json.dumps(found, indent=1) + "\n", encoding="utf-8")
    counts: dict[str, int] = {}
    for effect in found:
        counts[effect["colour"]] = counts.get(effect["colour"], 0) + 1
    print(f"{len(found)} effects -> {OUT}: {counts}")


if __name__ == "__main__":
    main(sys.argv[1])
