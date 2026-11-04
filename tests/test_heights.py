import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from eqoa_server import heights


def make_map(path: Path, top, extras=(), world=0, x0=100.0, z0=200.0, step=8.0):
    """A height map file as tools/heightmap writes it: top[j][i] heights (None: no ground),
    extras [(i, j, y)] lower floors."""
    nz, nx = len(top), len(top[0])
    out = b"TRHM" + struct.pack("<II", 1, 1)
    name = b"TUNARIA.ESF"
    out += struct.pack("<IH", world, len(name)) + name + struct.pack("<fffII", x0, z0, step, nx, nz)
    out += b"".join(struct.pack("<h", heights.NONE if y is None else round(y * 16)) for row in top for y in row)
    cells = sorted((j * nx + i, y) for i, j, y in extras)
    out += struct.pack("<I", len(cells)) + b"".join(struct.pack("<I", c) for c, _ in cells)
    out += b"".join(struct.pack("<h", round(y * 16)) for _, y in cells)
    path.write_bytes(zlib.compress(out))
    return path


class HeightTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())

    def test_blends_between_grid_points_on_a_slope(self):
        hm = heights.read(make_map(self.folder / "h.bin", [[50.0, 52.0], [54.0, 56.0]]))
        self.assertAlmostEqual(hm.ground(0, 100.0, 200.0), 50.0)
        self.assertAlmostEqual(hm.ground(0, 104.0, 200.0), 51.0)  # halfway along x
        self.assertAlmostEqual(hm.ground(0, 104.0, 204.0), 53.0)  # the middle
        self.assertIsNone(hm.ground(0, 5000.0, 5000.0))  # off the map
        self.assertIsNone(hm.ground(3, 100.0, 200.0))  # another world

    def test_stands_on_the_floor_under_it_not_the_roof(self):
        # a house: roof at 70 over a floor at 50 on the left, open ground at 50 on the right
        hm = heights.read(make_map(self.folder / "h.bin", [[70.0, 50.0], [70.0, 50.0]],
                                   extras=[(0, 0, 50.0), (0, 1, 50.0)]))
        self.assertAlmostEqual(hm.ground(0, 100.0, 200.0, near=50.5), 50.0)  # inside: the floor
        self.assertAlmostEqual(hm.ground(0, 100.0, 200.0, near=70.2), 70.0)  # up on the roof: the roof
        self.assertAlmostEqual(hm.ground(0, 100.0, 200.0), 70.0)  # no height to go by: the top
        # walking out of the door: no blending the roof into the floor
        self.assertAlmostEqual(hm.ground(0, 102.0, 200.0, near=50.0), 50.0)

    def test_a_wall_or_cliff_isnt_blended(self):
        hm = heights.read(make_map(self.folder / "h.bin", [[50.0, 90.0], [50.0, 90.0]]))
        self.assertAlmostEqual(hm.ground(0, 101.0, 200.0, near=50.0), 50.0)  # the near side's height

    def test_load_reads_again_when_the_file_changes_and_survives_a_bad_one(self):
        path = make_map(self.folder / "h.bin", [[50.0, 50.0], [50.0, 50.0]])
        self.assertAlmostEqual(heights.load(path).ground(0, 104.0, 204.0), 50.0)
        import os
        import time

        make_map(path, [[60.0, 60.0], [60.0, 60.0]])
        os.utime(path, (time.time() + 5, time.time() + 5))
        self.assertAlmostEqual(heights.load(path).ground(0, 104.0, 204.0), 60.0)
        bad = self.folder / "bad.bin"
        bad.write_bytes(b"nonsense")
        self.assertIsNone(heights.load(bad))
        self.assertIsNone(heights.load(self.folder / "missing.bin"))

    def test_a_bot_stands_on_the_map_ground(self):
        from eqoa_server import bot_client

        path = make_map(self.folder / "h.bin", [[50.0, 52.0], [54.0, 56.0]])
        bot = bot_client.BotClient("127.0.0.1", heights_path=path, log=lambda line: None)
        bot.position = [0, 104.0, 999.0, 204.0, 0.0]
        self.assertAlmostEqual(bot.ground_y(104.0, 204.0), 53.0)


class HeightmapJobTests(unittest.TestCase):
    def test_runs_the_program_on_the_disc_and_the_map_is_used(self):
        import sys
        import time
        from unittest import mock

        from eqoa_server.heightmap_job import HeightmapJob

        folder = Path(tempfile.mkdtemp())
        iso = folder / "frontiers.iso"
        iso.write_bytes(b"not really a disc")
        # a stand-in for the built program: writes a small map where it's told
        fake = folder / "fake-heightmap"
        fake.write_text(f"#!{sys.executable}\n"
                        "import sys\n"
                        f"sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})\n"
                        "from pathlib import Path\n"
                        "from tests.test_heights import make_map\n"
                        "print('TUNARIA.ESF: 2 x 2 points')\n"
                        "make_map(Path(sys.argv[2]), [[50.0, 50.0], [50.0, 50.0]])\n")
        fake.chmod(0o755)
        job = HeightmapJob(folder)
        self.assertIsNone(job.status()["loaded"])
        self.assertIn(str(iso), job.status()["isos"])
        self.assertEqual(job.start(str(folder / "missing.iso"))[0], False)
        with mock.patch.object(HeightmapJob, "_fetch", return_value=fake), \
                mock.patch("eqoa_server.heightmap_job.PROGRAMS", {}), \
                mock.patch("eqoa_server.heightmap_job.platform.system", return_value="Linux"):
            self.assertEqual(job.start(str(iso))[0], False)  # no program for this machine
        with mock.patch.object(HeightmapJob, "_fetch", return_value=fake):
            self.assertTrue(job.start(str(iso))[0])
            for _ in range(100):
                if not job.running:
                    break
                time.sleep(0.1)
        status = job.status()
        self.assertTrue(job.ok, status["lines"])
        self.assertIn("TUNARIA.ESF", status["loaded"])
        self.assertTrue(any("2 x 2 points" in line for line in status["lines"]))


if __name__ == "__main__":
    unittest.main()
