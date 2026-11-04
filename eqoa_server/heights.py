"""Ground heights of the worlds, from data/heights.bin.

The file is made once from your own Frontiers disc image by tools/heightmap (the
game's own zone reader and floor probe; its doc has the format). It never goes in the
repository: it's made from the disc. Without it, callers fall back to guessing (bots
guess from the NPCs around them).

Each grid point has its highest floor and, where there are more (a building's floors
and roof, a bridge, a tree over the grass, a cave under a hill), the ones below. A
height is asked for near a height (where the character is now): the floor it stands on
is the highest one at most STEP_UP above that, else the lowest.
"""
from __future__ import annotations

import bisect
import math
import struct
import threading
import zlib
from array import array
from pathlib import Path

NONE = -32768
STEP_UP = 3.0  # a character climbs onto a floor this far above its feet
CLIFF_SLOPE = 1.5  # corners further apart than this x the grid step aren't blended (steeper than
# any walkable slope: a wall's foot, a roof's edge, a cliff)


class WorldHeights:
    def __init__(self, world: int, name: str, x0: float, z0: float, step: float, nx: int, nz: int,
                 top: array, cells: array, extras: array) -> None:
        self.world, self.name = world, name
        self.x0, self.z0, self.step, self.nx, self.nz = x0, z0, step, nx, nz
        self.top, self.cells, self.extras = top, cells, extras

    def levels(self, i: int, j: int) -> list[float]:
        """Every floor at grid point (i, j), highest first."""
        if not (0 <= i < self.nx and 0 <= j < self.nz):
            return []
        cell = j * self.nx + i
        top = self.top[cell]
        if top == NONE:
            return []
        out = [top / 16.0]
        k = bisect.bisect_left(self.cells, cell)
        while k < len(self.cells) and self.cells[k] == cell:
            out.append(self.extras[k] / 16.0)
            k += 1
        return out

    @staticmethod
    def _pick(levels: list[float], near: float | None) -> float | None:
        if not levels:
            return None
        if near is None:
            return levels[0]
        for y in levels:  # highest first: the first not too far above is the one stood on
            if y <= near + STEP_UP:
                return y
        return levels[-1]

    def ground(self, x: float, z: float, near: float | None = None) -> float | None:
        fx, fz = (x - self.x0) / self.step, (z - self.z0) / self.step
        i, j = math.floor(fx), math.floor(fz)
        tx, tz = fx - i, fz - j
        corners = [(self._pick(self.levels(i + di, j + dj), near), (tx if di else 1 - tx) * (tz if dj else 1 - tz))
                   for di in (0, 1) for dj in (0, 1)]
        found = [(y, w) for y, w in corners if y is not None]
        if not found:
            return None
        nearest = max(found, key=lambda pair: pair[1])[0]
        same = [(y, w) for y, w in found if abs(y - nearest) <= CLIFF_SLOPE * self.step]
        weight = sum(w for _, w in same)
        if weight <= 0:
            return nearest
        return sum(y * w for y, w in same) / weight


class Heights:
    def __init__(self, worlds: dict[int, WorldHeights]) -> None:
        self.worlds = worlds

    def ground(self, world: int, x: float, z: float, near: float | None = None) -> float | None:
        grid = self.worlds.get(int(world))
        return grid.ground(x, z, near) if grid else None

    def describe(self) -> str:
        return ", ".join(f"{g.name} ({g.nx}x{g.nz} every {g.step:g})" for g in self.worlds.values())


def read(path: Path) -> Heights:
    data = zlib.decompress(Path(path).read_bytes())
    if data[:4] != b"TRHM":
        raise ValueError(f"{path} isn't a height map")
    version, count = struct.unpack_from("<II", data, 4)
    if version != 1:
        raise ValueError(f"{path}: height map version {version}, this server reads 1")
    at, worlds = 12, {}
    for _ in range(count):
        world, name_length = struct.unpack_from("<IH", data, at)
        at += 6
        name = data[at:at + name_length].decode("ascii", "replace")
        at += name_length
        x0, z0, step, nx, nz = struct.unpack_from("<fffII", data, at)
        at += 20
        top = array("h")
        top.frombytes(data[at:at + 2 * nx * nz])
        at += 2 * nx * nz
        (extra_count,) = struct.unpack_from("<I", data, at)
        at += 4
        cells, extras = array("I"), array("h")
        cells.frombytes(data[at:at + 4 * extra_count])
        at += 4 * extra_count
        extras.frombytes(data[at:at + 2 * extra_count])
        at += 2 * extra_count
        worlds[world] = WorldHeights(world, name, x0, z0, step, nx, nz, top, cells, extras)
    return Heights(worlds)


_cache: dict[Path, tuple[float, Heights | None]] = {}
_lock = threading.Lock()


def load(path: Path) -> Heights | None:
    """The height map at ``path``, read once and again when the file changes; None
    without one (or an unreadable one, said once)."""
    path = Path(path)
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return None
    with _lock:
        cached = _cache.get(path)
        if cached and cached[0] == stamp:
            return cached[1]
        try:
            heights = read(path)
            print(f"HEIGHTS loaded {path}: {heights.describe()}", flush=True)
        except (OSError, ValueError, zlib.error, struct.error) as exc:
            print(f"HEIGHTS {path} unreadable: {exc}", flush=True)
            heights = None
        _cache[path] = (stamp, heights)
        return heights


FILE_NAME = "heights.bin"


def install(data: bytes, data_dir: Path, store=None) -> Heights:
    """Put an uploaded height map in place (checked first), and in the database when the
    save lives there (a host whose files don't last, like Render's free services)."""
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    tmp = data_dir / (FILE_NAME + ".upload")
    tmp.write_bytes(data)
    try:
        heights = read(tmp)
    except (ValueError, zlib.error, struct.error) as exc:
        tmp.unlink(missing_ok=True)
        raise ValueError(f"that isn't a height map from tools/heightmap ({exc})") from None
    tmp.replace(data_dir / FILE_NAME)
    if store is not None and getattr(store, "database", None) is not None:
        store.database.put_file(FILE_NAME, data)
    return heights


def restore(data_dir: Path, store) -> bool:
    """On a host whose files don't last: bring the height map back from the database."""
    path = Path(data_dir) / FILE_NAME
    if path.exists() or getattr(store, "database", None) is None:
        return False
    try:
        data = store.database.get_file(FILE_NAME)
    except Exception as exc:  # noqa: BLE001 - the server runs without it
        print(f"HEIGHTS couldn't read the height map from the database: {exc}", flush=True)
        return False
    if data is None:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    print(f"HEIGHTS restored {path} from the database ({len(data) // 1024} KB)", flush=True)
    return True
