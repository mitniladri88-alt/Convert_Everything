"""Font converters: TTF, OTF, WOFF and WOFF2 to each other.

Pure Python: fontTools (already installed with pdf2docx) plus brotli for WOFF2.

A font file is two things: the outlines inside it and the container around them.

* TTF holds TrueType (quadratic) outlines, OTF holds CFF (cubic) outlines.
  Converting between them redraws every glyph: cubic curves are approximated
  with quadratics (cu2qu, within 1 font unit) or quadratics are written as
  exact cubics. Layout features, kerning, names and metrics are kept;
  TrueType hinting instructions can't carry across, so they are dropped.
* WOFF and WOFF2 are compressed wrappers for the web. Converting to them
  keeps the outlines as they are (a WOFF made from an OTF still holds CFF).

Fonts come from strangers, so the size a WOFF/WOFF2 claims it unpacks to is
checked before anything is decompressed. Variable fonts can be rewrapped
(WOFF/WOFF2) but their outlines aren't converted between TTF and OTF.
"""
import os
import struct
from pathlib import Path

from errors import ConversionError

FONT_IN = ["ttf", "otf", "woff", "woff2"]
FONT_OUT = ["ttf", "otf", "woff", "woff2"]

MAX_UNPACKED_BYTES = int(os.environ.get("MAX_FONT_UNPACKED_MB", "128")) * 1024 * 1024

# Tables that only make sense next to TrueType outlines (hinting, glyf index).
TRUETYPE_ONLY = ["glyf", "loca", "fpgm", "prep", "cvt ", "gasp", "hdmx", "LTSH", "VDMX", "cvar"]
VARIATION_TABLES = ["fvar", "gvar", "CFF2"]


def targets_for(ext: str) -> list[str]:
    return [t for t in FONT_OUT if t != ext]


# ---------- Safety ----------
def _check_unpacked_size(src: Path) -> None:
    """WOFF and WOFF2 headers say how big the decompressed font is (totalSfntSize,
    bytes 16-20). Refuse obvious bombs before fontTools inflates anything."""
    with open(src, "rb") as f:
        header = f.read(20)
    if len(header) < 20 or header[:4] not in (b"wOFF", b"wOF2"):
        return
    (total,) = struct.unpack(">I", header[16:20])
    if total > MAX_UNPACKED_BYTES:
        mb = MAX_UNPACKED_BYTES // (1024 * 1024)
        raise ConversionError(f"{src.name} unpacks to more than {mb} MB, which is over the limit for fonts.")


def _open(src: Path):
    from fontTools.ttLib import TTFont, TTLibError
    _check_unpacked_size(src)
    try:
        font = TTFont(str(src), lazy=False)
        font.getGlyphOrder()
    except TTLibError as exc:
        msg = str(exc)
        if "collection" in msg.lower():
            raise ConversionError(f"{src.name} is a font collection (several fonts in one file), which isn't supported.")
        raise ConversionError(f"Can't read {src.name} as a font ({msg}).")
    except Exception as exc:
        raise ConversionError(f"Can't read {src.name} as a font ({exc}).")
    if "glyf" not in font and "CFF " not in font and "CFF2" not in font:
        raise ConversionError(f"{src.name} has no glyph outlines (it may be a bitmap-only or SVG-only font).")
    return font


def _refuse_variable(font, src: Path, target: str) -> None:
    if any(tag in font for tag in VARIATION_TABLES):
        raise ConversionError(
            f"{src.name} is a variable font. Its outlines can't be changed to {target.upper()} here, "
            "but it converts to WOFF or WOFF2 as it is."
        )


# ---------- Outline conversion ----------
def _cff_to_truetype(font) -> None:
    """CFF (cubic) -> glyf (quadratic), in place. Based on fontTools' otf2ttf snippet."""
    from fontTools.pens.cu2quPen import Cu2QuPen
    from fontTools.pens.ttGlyphPen import TTGlyphPen
    from fontTools.ttLib import newTable

    glyph_order = font.getGlyphOrder()
    glyph_set = font.getGlyphSet()
    glyphs = {}
    for name in glyph_order:
        tt_pen = TTGlyphPen(glyph_set)
        # CFF contours run counter-clockwise, TrueType clockwise: reverse while converting.
        glyph_set[name].draw(Cu2QuPen(tt_pen, max_err=1.0, reverse_direction=True))
        glyphs[name] = tt_pen.glyph()

    font["loca"] = newTable("loca")
    glyf = font["glyf"] = newTable("glyf")
    glyf.glyphOrder = glyph_order
    glyf.glyphs = glyphs
    for tag in ("CFF ", "VORG"):
        if tag in font:
            del font[tag]
    glyf.compile(font)

    hmtx = font["hmtx"]
    for name, glyph in glyphs.items():
        if hasattr(glyph, "xMin"):
            hmtx[name] = (hmtx[name][0], glyph.xMin)  # left side bearing = leftmost point

    maxp = font["maxp"] = newTable("maxp")
    maxp.tableVersion = 0x00010000
    maxp.numGlyphs = len(glyph_order)
    maxp.maxZones = 1
    for attr in ("maxTwilightPoints", "maxStorage", "maxFunctionDefs", "maxInstructionDefs",
                 "maxStackElements", "maxSizeOfInstructions"):
        setattr(maxp, attr, 0)
    maxp.maxComponentElements = max((len(getattr(g, "components", [])) for g in glyphs.values()), default=0)
    maxp.compile(font)  # fills in the point and contour counts

    post = font["post"]
    post.formatType = 2.0  # TrueType fonts carry glyph names in post
    post.extraNames = []
    post.mapping = {}
    post.glyphOrder = glyph_order
    font.sfntVersion = "\x00\x01\x00\x00"


def _ps_name(font) -> str:
    name = font["name"].getDebugName(6) or font["name"].getDebugName(4) or "Font"
    return "".join(c for c in name if 33 <= ord(c) <= 126 and c not in "[](){}<>/%")[:63] or "Font"


def _truetype_to_cff(font) -> None:
    """glyf (quadratic) -> CFF (cubic), in place. Every quadratic curve is an exact cubic."""
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.t2CharStringPen import T2CharStringPen
    from fontTools.pens.reverseContourPen import ReverseContourPen
    from fontTools.ttLib import newTable

    glyph_order = font.getGlyphOrder()
    glyph_set = font.getGlyphSet()  # draws composite glyphs as plain outlines
    hmtx = font["hmtx"]
    charstrings = {}
    for name in glyph_order:
        pen = T2CharStringPen(width=hmtx[name][0], glyphSet=glyph_set)
        glyph_set[name].draw(ReverseContourPen(pen))  # back to counter-clockwise for CFF
        charstrings[name] = pen.getCharString()

    names = font["name"]
    info = {
        "FullName": names.getDebugName(4) or "",
        "FamilyName": names.getDebugName(1) or "",
        "Weight": names.getDebugName(2) or "Regular",
    }
    if names.getDebugName(0):
        info["Notice"] = names.getDebugName(0)
    if "post" in font:
        post = font["post"]
        info.update(ItalicAngle=post.italicAngle, UnderlinePosition=post.underlinePosition,
                    UnderlineThickness=post.underlineThickness, isFixedPitch=post.isFixedPitch)

    for tag in TRUETYPE_ONLY:
        if tag in font:
            del font[tag]
    fb = FontBuilder(font=font)
    fb.isTTF = False
    fb.setupCFF(_ps_name(font), info, charstrings, {"defaultWidthX": 0, "nominalWidthX": 0})

    maxp = font["maxp"] = newTable("maxp")
    maxp.tableVersion = 0x00005000
    maxp.numGlyphs = len(glyph_order)

    post = font["post"]
    post.formatType = 3.0  # CFF keeps the glyph names itself
    post.extraNames = []
    post.mapping = {}
    font.sfntVersion = "OTTO"


# ---------- Entry point ----------
def convert_font(src: Path, target: str, out_dir: Path) -> list[Path]:
    font = _open(src)
    out = out_dir / f"{src.stem}.{target}"
    try:
        is_truetype = "glyf" in font
        if target == "ttf" and not is_truetype:
            _refuse_variable(font, src, target)
            _cff_to_truetype(font)
        elif target == "otf" and is_truetype:
            _refuse_variable(font, src, target)
            _truetype_to_cff(font)
        font.flavor = target if target in ("woff", "woff2") else None
        font.save(str(out))
    except ConversionError:
        raise
    except ImportError as exc:  # brotli missing
        raise ConversionError(f"WOFF2 support isn't installed on the server ({exc}).")
    except Exception as exc:
        raise ConversionError(f"Couldn't convert {src.name} to {target.upper()} ({exc}).")
    finally:
        font.close()
    return [out]
