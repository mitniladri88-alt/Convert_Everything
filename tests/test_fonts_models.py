"""Tests for the Font and 3D model converters.

Both are pure Python (fontTools + brotli, trimesh + numpy), so these tests need
no system tools. Fixtures are built in code. Run with:
    python -m unittest discover -s tests -v
"""
import io
import shutil
import struct
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
import trimesh  # noqa: E402
from fontTools.fontBuilder import FontBuilder  # noqa: E402
from fontTools.pens.boundsPen import BoundsPen  # noqa: E402
from fontTools.pens.t2CharStringPen import T2CharStringPen  # noqa: E402
from fontTools.pens.ttGlyphPen import TTGlyphPen  # noqa: E402
from fontTools.ttLib import TTFont  # noqa: E402

import font_converter  # noqa: E402
import model_converter  # noqa: E402
from app import app  # noqa: E402
from converter import CATEGORIES, convert_file  # noqa: E402
from errors import ConversionError  # noqa: E402

GLYPHS = [".notdef", "space", "A", "O", "Aacute"]
CMAP = {0x20: "space", 0x41: "A", 0x4F: "O", 0xC1: "Aacute"}
SIGNATURES = {"ttf": b"\x00\x01\x00\x00", "otf": b"OTTO", "woff": b"wOFF", "woff2": b"wOF2"}


def _draw_a(pen):  # straight lines
    pen.moveTo((50, 0)); pen.lineTo((300, 700)); pen.lineTo((550, 0)); pen.closePath()


def _draw_box(pen):
    pen.moveTo((100, 0)); pen.lineTo((100, 100)); pen.lineTo((200, 100)); pen.lineTo((200, 0)); pen.closePath()


def _finish(fb, family, lsb=None):
    fb.setupCharacterMap(CMAP)
    fb.setupHorizontalMetrics({g: (600, (lsb or {}).get(g, 0)) for g in GLYPHS})
    fb.setupHorizontalHeader(ascent=800, descent=-200)
    fb.setupNameTable({"familyName": family, "styleName": "Regular"})
    fb.setupOS2(sTypoAscender=800, usWinAscent=800, usWinDescent=200)
    fb.setupPost()


def make_ttf(path: Path, variable: bool = False) -> Path:
    """Quadratic curves, a composite glyph (Aacute = A + box) and a kern-free layout."""
    fb = FontBuilder(1000, isTTF=True)
    fb.setupGlyphOrder(GLYPHS)
    glyphs = {}
    for name in (".notdef", "space"):
        glyphs[name] = TTGlyphPen(None).glyph()
    pen = TTGlyphPen(None); _draw_a(pen); glyphs["A"] = pen.glyph()
    pen = TTGlyphPen(None)
    pen.moveTo((300, 0)); pen.qCurveTo((550, 0), (550, 350)); pen.qCurveTo((550, 700), (300, 700))
    pen.qCurveTo((50, 700), (50, 350)); pen.qCurveTo((50, 0), (300, 0)); pen.closePath()
    glyphs["O"] = pen.glyph()
    pen = TTGlyphPen(glyphs); pen.addComponent("A", (1, 0, 0, 1, 0, 0)); pen.addComponent("O", (0.2, 0, 0, 0.2, 240, 720))
    glyphs["Aacute"] = pen.glyph()
    fb.setupGlyf(glyphs)
    glyf = fb.font["glyf"]
    lsb = {}
    for name in GLYPHS:  # left side bearing = leftmost point, as in real fonts
        glyf[name].recalcBounds(glyf)
        lsb[name] = getattr(glyf[name], "xMin", 0)
    _finish(fb, "Test Sans", lsb)
    if variable:
        fb.setupFvar([("wght", 100, 400, 900, "Weight")], [])
    fb.save(str(path))
    return path


def make_otf(path: Path) -> Path:
    """Cubic curves in CFF."""
    fb = FontBuilder(1000, isTTF=False)
    fb.setupGlyphOrder(GLYPHS)
    charstrings = {}
    for name in GLYPHS:
        pen = T2CharStringPen(600, None)
        if name == "A":
            _draw_a(pen)
        elif name in ("O", "Aacute"):
            pen.moveTo((300, 0)); pen.curveTo((440, 0), (550, 160), (550, 350)); pen.curveTo((550, 540), (440, 700), (300, 700))
            pen.curveTo((160, 700), (50, 540), (50, 350)); pen.curveTo((50, 160), (160, 0), (300, 0)); pen.closePath()
            if name == "Aacute":
                _draw_box(pen)
        charstrings[name] = pen.getCharString()
    fb.setupCFF("TestSerif-Regular", {"FullName": "Test Serif"}, charstrings, {})
    _finish(fb, "Test Serif")
    fb.save(str(path))
    return path


def bounds(font: TTFont, name: str):
    pen = BoundsPen(font.getGlyphSet())
    font.getGlyphSet()[name].draw(pen)
    return pen.bounds


class FontTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.out = self.dir / "out"
        self.out.mkdir()
        ttf = make_ttf(self.dir / "sans.ttf")
        otf = make_otf(self.dir / "serif.otf")
        self.fx = {"ttf": ttf, "otf": otf}
        for flavor in ("woff", "woff2"):
            f = TTFont(str(ttf)); f.flavor = flavor; f.save(str(self.dir / f"sans.{flavor}"))
            self.fx[flavor] = self.dir / f"sans.{flavor}"

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_registered_as_its_own_converter(self):
        self.assertEqual(set(CATEGORIES["font"]), {"ttf", "otf", "woff", "woff2"})
        self.assertEqual(CATEGORIES["font"]["woff2"], ["ttf", "otf", "woff"])

    def test_every_pair_converts_and_keeps_glyphs(self):
        for ext, src in self.fx.items():
            original = TTFont(str(src))
            for target in CATEGORIES["font"][ext]:
                with self.subTest(f"{ext} -> {target}"):
                    d = self.out / f"{ext}_{target}"; d.mkdir()
                    [out] = convert_file(src, target, d, category="font")
                    self.assertEqual(out.suffix, f".{target}")
                    self.assertTrue(out.read_bytes().startswith(SIGNATURES[target]))
                    font = TTFont(str(out))
                    self.assertEqual(font.getGlyphOrder(), original.getGlyphOrder())
                    self.assertEqual(font.getBestCmap(), original.getBestCmap())
                    if target == "ttf":
                        self.assertIn("glyf", font)
                    if target == "otf":
                        self.assertIn("CFF ", font)
                    for name in ("A", "O", "Aacute"):  # outlines stay within cu2qu's 1-unit error
                        for a, b in zip(bounds(font, name), bounds(original, name)):
                            self.assertAlmostEqual(a, b, delta=2)

    def test_web_formats_keep_outline_type(self):
        [out] = font_converter.convert_font(self.fx["otf"], "woff2", self.out)
        self.assertIn("CFF ", TTFont(str(out)))

    def test_ttf_to_otf_to_ttf_round_trip(self):
        [otf] = font_converter.convert_font(self.fx["ttf"], "otf", self.out)
        back_dir = self.out / "back"; back_dir.mkdir()
        [ttf] = font_converter.convert_font(otf, "ttf", back_dir)
        font = TTFont(str(ttf))
        self.assertEqual(font["maxp"].numGlyphs, len(GLYPHS))
        self.assertEqual(font["name"].getDebugName(1), "Test Sans")

    def test_variable_font_only_rewraps(self):
        var = make_ttf(self.dir / "var.ttf", variable=True)
        with self.assertRaisesRegex(ConversionError, "variable font"):
            font_converter.convert_font(var, "otf", self.out)
        [out] = font_converter.convert_font(var, "woff2", self.out)
        self.assertIn("fvar", TTFont(str(out)))

    def test_woff_bomb_refused_before_unpacking(self):
        data = bytearray(self.fx["woff"].read_bytes())
        data[16:20] = struct.pack(">I", 0xFFFFFFFF)
        bomb = self.dir / "bomb.woff"
        bomb.write_bytes(bytes(data))
        with self.assertRaisesRegex(ConversionError, "over the limit"):
            font_converter.convert_font(bomb, "ttf", self.out)

    def test_renamed_or_broken_files_refused(self):
        fake = self.dir / "fake.ttf"
        fake.write_bytes(b"not a font at all")
        with self.assertRaisesRegex(ConversionError, "doesn't look like a real"):
            convert_file(fake, "woff", self.out, category="font")
        broken = self.dir / "broken.woff2"
        broken.write_bytes(b"wOF2" + b"\x00" * 60)
        with self.assertRaises(ConversionError):
            convert_file(broken, "ttf", self.out, category="font")


def make_models(d: Path) -> dict:
    box = trimesh.creation.box()
    box.visual.face_colors = np.tile([200, 30, 30, 255], (len(box.faces), 1))
    ball = trimesh.creation.icosphere(subdivisions=1)
    ball.apply_translation([3, 0, 0])
    fx = {}
    fx["stl"] = d / "cube.stl"; fx["stl"].write_bytes(trimesh.creation.box().export(file_type="stl"))
    fx["obj"] = d / "tri.obj"; fx["obj"].write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\nv 0 0 1\nf 1 2 3\nf 1 2 4\n")
    fx["ply"] = d / "colored.ply"; fx["ply"].write_bytes(box.export(file_type="ply"))  # per-face colours
    part = trimesh.creation.box()
    part.visual.vertex_colors = [30, 30, 200, 255]  # trimesh's own face colour export needs scipy
    fx["glb"] = d / "parts.glb"; fx["glb"].write_bytes(trimesh.Scene({"box": part, "ball": ball}).export(file_type="glb"))
    return fx


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.out = self.dir / "out"
        self.out.mkdir()
        self.fx = make_models(self.dir)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_registered_as_its_own_converter(self):
        self.assertEqual(set(CATEGORIES["model"]), {"stl", "obj", "ply", "glb"})

    def test_every_pair_converts_and_keeps_shape(self):
        for ext, src in self.fx.items():
            original = trimesh.load(str(src))
            for target in CATEGORIES["model"][ext]:
                with self.subTest(f"{ext} -> {target}"):
                    d = self.out / f"{ext}_{target}"; d.mkdir()
                    outs = convert_file(src, target, d, category="model")
                    self.assertEqual(outs[0].suffix, f".{target}")
                    back = trimesh.load(str(outs[0]))
                    np.testing.assert_allclose(back.bounds, original.bounds, atol=1e-5)
                    faces = sum(len(g.faces) for g in back.geometry.values()) if isinstance(back, trimesh.Scene) else len(back.faces)
                    orig_faces = sum(len(g.faces) for g in original.geometry.values()) if isinstance(original, trimesh.Scene) else len(original.faces)
                    self.assertEqual(faces, orig_faces)

    def test_parts_merge_for_stl_and_stay_separate_in_obj(self):
        [stl] = model_converter.convert_model(self.fx["glb"], "stl", self.out)
        self.assertIsInstance(trimesh.load(str(stl)), trimesh.Trimesh)
        [obj] = model_converter.convert_model(self.fx["glb"], "obj", self.out)
        self.assertEqual(sorted(l.split()[1] for l in obj.read_text().splitlines() if l.startswith("o ")), ["ball", "box"])

    def test_face_colours_survive_to_glb(self):
        [glb] = model_converter.convert_model(self.fx["ply"], "glb", self.out)
        mesh = next(iter(trimesh.load(str(glb)).geometry.values()))
        self.assertTrue((mesh.visual.vertex_colors[:, :3] == [200, 30, 30]).all())

    def test_textured_model_to_obj_returns_mtl_and_image(self):
        from PIL import Image
        m = trimesh.creation.box()
        m.visual = trimesh.visual.TextureVisuals(uv=np.random.rand(len(m.vertices), 2), image=Image.new("RGB", (4, 4), "blue"))
        src = self.dir / "textured.glb"
        src.write_bytes(trimesh.Scene(m).export(file_type="glb"))
        outs = model_converter.convert_model(src, "obj", self.out)
        names = sorted(p.name for p in outs)
        self.assertIn("textured.obj", names)
        self.assertIn("textured.mtl", names)
        self.assertTrue(any(n.endswith(".png") for n in names))
        self.assertTrue(all(p.parent == self.out for p in outs))

    def test_obj_never_reads_other_files(self):
        secret = self.dir / "secret.mtl"
        secret.write_text("newmtl TOPSECRET\nKd 1 0 0\n")
        src = self.dir / "sneaky.obj"
        src.write_text(f"mtllib {secret}\nmtllib ../secret.mtl\nusemtl TOPSECRET\nv 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n")
        for target in ("obj", "glb"):
            for p in model_converter.convert_model(src, target, self.out):
                self.assertNotIn(b"TOPSECRET", p.read_bytes())

    def test_unsafe_and_empty_models_refused(self):
        bomb = self.dir / "bomb.ply"
        bomb.write_bytes(b"ply\nformat binary_little_endian 1.0\nelement vertex 999999999999\nproperty float x\nend_header\n")
        with self.assertRaisesRegex(ConversionError, "too large"):
            convert_file(bomb, "stl", self.out, category="model")
        points = self.dir / "points.ply"
        points.write_bytes(trimesh.PointCloud(np.random.rand(5, 3)).export(file_type="ply"))
        with self.assertRaisesRegex(ConversionError, "no surfaces"):
            convert_file(points, "stl", self.out, category="model")
        fake = self.dir / "fake.glb"
        fake.write_bytes(b"hello")
        with self.assertRaisesRegex(ConversionError, "doesn't look like a real"):
            convert_file(fake, "stl", self.out, category="model")

    def test_face_limit(self):
        old = model_converter.MAX_FACES
        model_converter.MAX_FACES = 5
        try:
            with self.assertRaisesRegex(ConversionError, "limit is 5"):
                model_converter.convert_model(self.fx["stl"], "obj", self.out)
        finally:
            model_converter.MAX_FACES = old


class ApiTests(unittest.TestCase):
    """The batch UI sends one request per file with a category, like these."""

    def setUp(self):
        self.client = app.test_client()
        self.dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def post(self, path: Path, target: str, category: str):
        return self.client.post("/api/convert", data={
            "files": [(io.BytesIO(path.read_bytes()), path.name)], "target": target, "category": category,
        }, content_type="multipart/form-data")

    def test_formats_lists_new_converters(self):
        cats = self.client.get("/api/formats").get_json()["categories"]
        self.assertEqual(cats["font"]["ttf"]["targets"], ["otf", "woff", "woff2"])
        self.assertTrue(cats["model"]["stl"]["available"])

    def test_font_and_model_requests(self):
        res = self.post(make_ttf(self.dir / "sans.ttf"), "woff2", "font")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertTrue(res.data.startswith(b"wOF2"))
        self.assertIn("sans.woff2", res.headers["Content-Disposition"])
        stl = make_models(self.dir)["stl"]
        res = self.post(stl, "glb", "model")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertTrue(res.data.startswith(b"glTF"))

    def test_multi_file_obj_comes_back_as_zip(self):
        from PIL import Image
        m = trimesh.creation.box()
        m.visual = trimesh.visual.TextureVisuals(uv=np.random.rand(len(m.vertices), 2), image=Image.new("RGB", (4, 4), "red"))
        src = self.dir / "tex.glb"
        src.write_bytes(trimesh.Scene(m).export(file_type="glb"))
        res = self.post(src, "obj", "model")
        self.assertEqual(res.status_code, 200, res.data)
        names = zipfile.ZipFile(io.BytesIO(res.data)).namelist()
        self.assertIn("tex.obj", names)
        self.assertIn("tex.mtl", names)

    def test_wrong_tab_refused(self):
        res = self.post(make_ttf(self.dir / "sans.ttf"), "stl", "model")
        self.assertEqual(res.status_code, 400)
        self.assertIn("Use the matching converter tab", res.get_json()["error"])

    def test_page_has_new_tabs(self):
        html = self.client.get("/").data.decode()
        self.assertIn("id: 'font'", html)
        self.assertIn("id: 'model'", html)


if __name__ == "__main__":
    unittest.main()
