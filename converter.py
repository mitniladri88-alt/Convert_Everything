"""Conversion engine for Convert Anything.

Every supported input extension maps to a category and a list of target
extensions. `convert_file` routes one file to the right engine:

* images    -> Pillow (plus pillow-heif for HEIC/HEIF input)
* audio     -> ffmpeg
* video     -> ffmpeg
* documents -> LibreOffice (office formats, PDF export), pandoc (Markdown,
               HTML, EPUB), PyMuPDF and pdf2docx (PDF input)
* data      -> data_converter.py (CSV, JSON, XML, YAML, XLSX, Markdown)
* archives  -> archive_converter.py (ZIP, TAR, TAR.GZ, 7z)
* fonts     -> font_converter.py (TTF, OTF, WOFF, WOFF2)
* 3D models -> model_converter.py (STL, OBJ, PLY, GLB)

An extension can belong to more than one converter (CSV is both a spreadsheet
and a data file). REGISTRY keeps each extension's first ("primary") category;
CATEGORIES lists every converter with its own extensions and targets.
"""
import shutil
import subprocess
import tempfile
import time
import uuid
import zipfile
import json
from pathlib import Path

import pymupdf
from PIL import Image, ImageSequence, features

import archive_converter
import data_converter
import font_converter
import model_converter
from errors import ConversionError

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
    HEIF_AVAILABLE = True
except ImportError:  # HEIC input is optional
    HEIF_AVAILABLE = False

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "converter_uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

OFFICE_TIMEOUT = 180
MEDIA_TIMEOUT = 900


# ---------- Format registry ----------
IMAGE_OUT = ["png", "jpg", "webp", "gif", "bmp", "tiff", "ico", "pdf"]
if features.check("avif"):
    IMAGE_OUT.insert(3, "avif")
IMAGE_IN = ["png", "jpg", "jpeg", "webp", "gif", "bmp", "tif", "tiff", "ico"]
if features.check("avif"):
    IMAGE_IN.append("avif")
if HEIF_AVAILABLE:
    IMAGE_IN += ["heic", "heif"]

AUDIO_OUT = ["mp3", "wav", "ogg", "flac", "m4a", "aac", "opus"]
AUDIO_IN = AUDIO_OUT + ["wma", "aiff", "aif", "amr"]

VIDEO_OUT = ["mp4", "webm", "mov", "mkv", "avi", "gif"]
VIDEO_AUDIO_OUT = ["mp3", "wav", "m4a"]  # "extract audio" targets
VIDEO_IN = ["mp4", "mov", "mkv", "webm", "avi", "m4v", "flv", "wmv", "mpeg", "mpg", "3gp", "ts"]

TEXT_DOC_IN = ["docx", "doc", "odt", "rtf", "txt", "md", "html", "htm"]
TEXT_DOC_OUT = ["pdf", "docx", "odt", "rtf", "txt", "md", "html", "epub"]
SHEET_IN = ["xlsx", "xls", "ods", "csv"]
SHEET_OUT = ["pdf", "xlsx", "ods", "csv", "html"]
SLIDE_IN = ["pptx", "ppt", "odp"]
SLIDE_OUT = ["pdf", "pptx", "odp", "png", "jpg"]
PDF_OUT = ["docx", "txt", "png", "jpg"]

# Pretty names for the UI.
LABELS = {
    "jpg": "JPG", "jpeg": "JPEG", "tif": "TIFF", "tiff": "TIFF", "md": "Markdown",
    "htm": "HTML", "html": "HTML", "txt": "Plain text", "docx": "Word (DOCX)",
    "doc": "Word 97 (DOC)", "odt": "OpenDocument Text", "rtf": "Rich Text",
    "xlsx": "Excel (XLSX)", "xls": "Excel 97 (XLS)", "ods": "OpenDocument Sheet",
    "pptx": "PowerPoint (PPTX)", "ppt": "PowerPoint 97 (PPT)", "odp": "OpenDocument Slides",
    "epub": "EPUB e-book", "m4a": "M4A (AAC)", "aac": "AAC", "opus": "Opus",
    "json": "JSON", "xml": "XML", "yaml": "YAML", "yml": "YAML",
    "zip": "ZIP", "tar": "TAR", "tar.gz": "TAR.GZ (gzip)", "tgz": "TAR.GZ (gzip)", "7z": "7-Zip (7z)",
    "ttf": "TrueType (TTF)", "otf": "OpenType (OTF)", "woff": "WOFF", "woff2": "WOFF2",
    "stl": "STL", "obj": "Wavefront OBJ", "ply": "PLY", "glb": "glTF binary (GLB)",
}


def _registry():
    """Returns (REGISTRY, CATEGORIES). See the module docstring."""
    reg, cats = {}, {}

    def add(exts, category, targets):
        for ext in exts:
            outs = targets(ext) if callable(targets) else targets
            outs = [t for t in outs if t != ext and not (ext == "jpeg" and t == "jpg") and not (ext == "tif" and t == "tiff")]
            cats.setdefault(category, {})[ext] = outs
            reg.setdefault(ext, {"category": category, "targets": outs})

    add(IMAGE_IN, "image", IMAGE_OUT)
    add(AUDIO_IN, "audio", AUDIO_OUT)
    add(VIDEO_IN, "video", VIDEO_OUT + VIDEO_AUDIO_OUT)
    add(TEXT_DOC_IN, "document", TEXT_DOC_OUT)
    add(SHEET_IN, "spreadsheet", SHEET_OUT)
    add(SLIDE_IN, "presentation", SLIDE_OUT)
    add(["pdf"], "pdf", PDF_OUT)
    add(data_converter.DATA_IN, "data", data_converter.targets_for)
    add(archive_converter.ARCHIVE_IN, "archive", archive_converter.targets_for)
    add(font_converter.FONT_IN, "font", font_converter.targets_for)
    add(model_converter.MODEL_IN, "model", model_converter.targets_for)
    # Same-format "conversion" is still useful for video/audio re-encoding or
    # image resizing, but hiding it keeps the picker short and obvious.
    return reg, cats


REGISTRY, CATEGORIES = _registry()

COMPOUND_EXTS = ("tar.gz",)


def ext_of(path) -> str:
    name = Path(path).name.lower()
    for compound in COMPOUND_EXTS:
        if name.endswith("." + compound):
            return compound
    return Path(path).suffix.lower().lstrip(".")


def resolve(ext: str, target: str, category: str | None = None):
    """Pick the converter for ext -> target: (category, targets), or (None, None).

    With a category (the UI always sends one) only that converter is used.
    Without one, the primary category wins unless only another converter
    offers the target (e.g. CSV -> JSON goes to the data converter)."""
    if category:
        targets = CATEGORIES.get(category, {}).get(ext)
        return (category, targets) if targets is not None else (None, None)
    owners = [(cat, exts[ext]) for cat, exts in CATEGORIES.items() if ext in exts]
    for cat, targets in owners:
        if target in targets:
            return cat, targets
    return owners[0] if owners else (None, None)


def label(ext: str) -> str:
    return LABELS.get(ext, ext.upper())


def tool_status() -> dict:
    return {
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "libreoffice": bool(find_libreoffice()),
        "pandoc": bool(shutil.which("pandoc")),
        "heif": HEIF_AVAILABLE,
    }


def formats_payload() -> dict:
    tools = tool_status()
    needs = {"audio": "ffmpeg", "video": "ffmpeg", "spreadsheet": "libreoffice", "presentation": "libreoffice"}
    out = {}
    for ext, info in REGISTRY.items():
        tool = needs.get(info["category"])
        available = tools.get(tool, True) if tool else True
        out[ext] = {**info, "available": available}
    categories = {}
    for cat, exts in CATEGORIES.items():
        tool = needs.get(cat)
        available = tools.get(tool, True) if tool else True
        categories[cat] = {ext: {"targets": targets, "available": available} for ext, targets in exts.items()}
    every = set(REGISTRY) | {t for exts in CATEGORIES.values() for ts in exts.values() for t in ts}
    return {"formats": out, "categories": categories, "labels": {e: label(e) for e in every}, "tools": tools}


# ---------- Shared helpers (same conventions as the PDF suite) ----------
_MAGIC = {
    "pdf": [b"%PDF"],
    "png": [b"\x89PNG\r\n\x1a\n"],
    "jpg": [b"\xff\xd8\xff"],
    "jpeg": [b"\xff\xd8\xff"],
    "gif": [b"GIF87a", b"GIF89a"],
    "bmp": [b"BM"],
    "docx": [b"PK"], "xlsx": [b"PK"], "pptx": [b"PK"],
    "odt": [b"PK"], "ods": [b"PK"], "odp": [b"PK"],
    "zip": [b"PK"], "7z": [b"7z\xbc\xaf\x27\x1c"], "tar.gz": [b"\x1f\x8b"], "tgz": [b"\x1f\x8b"],
    # A .ttf may hold CFF outlines and a .otf TrueType ones, so either signature is fine.
    "ttf": [b"\x00\x01\x00\x00", b"OTTO", b"true"], "otf": [b"OTTO", b"\x00\x01\x00\x00", b"true"],
    "woff": [b"wOFF"], "woff2": [b"wOF2"],
    "ply": [b"ply"], "glb": [b"glTF"],
}


def validate_file(path: Path) -> None:
    if path.stat().st_size == 0:
        raise ConversionError(f"{path.name} is empty.")
    expected = _MAGIC.get(ext_of(path))
    if not expected:
        return
    with open(path, "rb") as f:
        header = f.read(16)
    if not any(header.startswith(sig) for sig in expected):
        raise ConversionError(f"{path.name} doesn't look like a real .{ext_of(path)} file. It may be corrupted or renamed.")


def safe_name(name: str) -> str:
    name = Path(name or "file").name
    cleaned = "".join(c for c in name if c.isalnum() or c in " ._-").strip()
    return cleaned or "file"


def cleanup_old_jobs(max_age_seconds: int = 3600) -> None:
    cutoff = time.time() - max_age_seconds
    for child in UPLOAD_DIR.iterdir():
        try:
            if child.stat().st_mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True) if child.is_dir() else child.unlink(missing_ok=True)
        except OSError:
            pass


def _run(cmd, timeout, what):
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ConversionError(f"{what} took too long and was stopped.")
    except FileNotFoundError:
        raise ConversionError(f"{what} is not installed on the server.")
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip().splitlines()[-3:]
        raise ConversionError(f"{what} failed: " + " ".join(tail))
    return result


def _dest(src: Path, out_dir: Path, ext: str) -> Path:
    return out_dir / f"{src.stem}.{ext}"


# ---------- Images ----------
def convert_image(src: Path, target: str, out_dir: Path, quality: int = 90, max_size: int | None = None) -> list[Path]:
    try:
        img = Image.open(src)
        img.load()
    except Exception as exc:
        raise ConversionError(f"Can't read {src.name} as an image ({exc}).")

    animated = getattr(img, "is_animated", False) and target in ("gif", "webp")
    frames = [f.copy() for f in ImageSequence.Iterator(img)] if animated else [img]

    def prep(frame):
        if max_size and max(frame.size) > max_size:
            frame = frame.copy()
            frame.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
        if target in ("jpg", "bmp", "pdf") and frame.mode not in ("RGB", "L"):
            # Flatten transparency onto white instead of black.
            rgba = frame.convert("RGBA")
            bg = Image.new("RGB", rgba.size, (255, 255, 255))
            bg.paste(rgba, mask=rgba.split()[-1])
            frame = bg
        elif frame.mode in ("CMYK", "I;16", "I", "F") or (frame.mode == "P" and target not in ("gif", "png")):
            frame = frame.convert("RGBA")
        return frame

    frames = [prep(f) for f in frames]
    out = _dest(src, out_dir, target)
    fmt = {"jpg": "JPEG", "tiff": "TIFF", "ico": "ICO"}.get(target, target.upper())
    kwargs = {}
    if target in ("jpg", "webp", "avif"):
        kwargs["quality"] = max(1, min(100, int(quality)))
    if target == "jpg":
        kwargs["optimize"] = True
    if target == "pdf":
        kwargs["resolution"] = 150.0
    if target == "ico":
        frame = frames[0]
        sizes = [s for s in (16, 32, 48, 64, 128, 256) if s <= max(frame.size)] or [16]
        kwargs["sizes"] = [(s, s) for s in sizes]
    if animated and len(frames) > 1:
        kwargs.update(save_all=True, append_images=frames[1:], loop=img.info.get("loop", 0),
                      duration=img.info.get("duration", 100))
    try:
        frames[0].save(out, format=fmt, **kwargs)
    except Exception as exc:
        raise ConversionError(f"Couldn't save {src.name} as {label(target)} ({exc}).")
    return [out]


def images_to_single_pdf(srcs: list[Path], out_dir: Path) -> Path:
    pages = []
    for src in srcs:
        page = convert_image(src, "png", out_dir)[0]
        pages.append(Image.open(page).convert("RGB"))
    out = out_dir / "combined.pdf"
    pages[0].save(out, format="PDF", save_all=True, append_images=pages[1:], resolution=150.0)
    return out


# ---------- Audio / video ----------
def probe(src: Path) -> dict:
    result = _run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "json", str(src)], 60, "Reading the media file")
    try:
        streams = json.loads(result.stdout).get("streams", [])
    except json.JSONDecodeError:
        streams = []
    types = {s.get("codec_type") for s in streams}
    return {"audio": "audio" in types, "video": "video" in types}


AUDIO_CODECS = {
    "mp3": ["-c:a", "libmp3lame"],
    "wav": ["-c:a", "pcm_s16le"],
    "ogg": ["-c:a", "libvorbis"],
    "flac": ["-c:a", "flac"],
    "m4a": ["-c:a", "aac", "-f", "ipod"],
    "aac": ["-c:a", "aac", "-f", "adts"],
    "opus": ["-c:a", "libopus"],
}
LOSSLESS_AUDIO = {"wav", "flac"}

VIDEO_CODECS = {
    "mp4": ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart"],
    "mov": ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart"],
    "mkv": ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k"],
    "webm": ["-c:v", "libvpx-vp9", "-crf", "34", "-b:v", "0", "-deadline", "realtime", "-cpu-used", "8", "-row-mt", "1", "-c:a", "libopus", "-b:a", "128k"],
    "avi": ["-c:v", "mpeg4", "-q:v", "4", "-c:a", "libmp3lame", "-b:a", "192k"],
}


def convert_media(src: Path, target: str, out_dir: Path, audio_bitrate: str = "192k", max_height: int | None = None) -> list[Path]:
    info = probe(src)
    out = _dest(src, out_dir, target)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src)]

    if target in AUDIO_CODECS:
        if not info["audio"]:
            raise ConversionError(f"{src.name} has no audio track to convert.")
        cmd += ["-vn", "-map", "0:a:0"] + AUDIO_CODECS[target]
        if target not in LOSSLESS_AUDIO:
            cmd += ["-b:a", audio_bitrate]
    elif target == "gif":
        if not info["video"]:
            raise ConversionError(f"{src.name} has no video to turn into a GIF.")
        h = min(max_height or 480, 480)
        cmd += ["-vf", f"fps=12,scale=-2:'min({h},ih)':flags=lanczos,split[a][b];[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4", "-loop", "0"]
    elif target in VIDEO_CODECS:
        if not info["video"]:
            raise ConversionError(f"{src.name} has no video track. Pick an audio format instead.")
        cmd += ["-map", "0:v:0", "-map", "0:a:0?"]
        if max_height:
            cmd += ["-vf", f"scale=-2:'min({int(max_height)},ih)'"]
        else:
            cmd += ["-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2"]  # x264 needs even sizes
        cmd += VIDEO_CODECS[target]
    else:
        raise ConversionError(f"Can't convert media to {label(target)}.")

    cmd.append(str(out))
    _run(cmd, MEDIA_TIMEOUT, "ffmpeg")
    if not out.is_file() or out.stat().st_size == 0:
        raise ConversionError("ffmpeg produced an empty file.")
    return [out]


# ---------- Documents ----------
def find_libreoffice():
    for item in (shutil.which("soffice"), shutil.which("libreoffice"),
                 r"C:\Program Files\LibreOffice\program\soffice.exe",
                 r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
                 "/Applications/LibreOffice.app/Contents/MacOS/soffice"):
        if item and Path(item).is_file():
            return str(item)
    return None


# LibreOffice output filters, so it always picks the right exporter.
LO_FILTERS = {
    "pdf": "pdf",
    "docx": 'docx:"MS Word 2007 XML"',
    "odt": "odt",
    "rtf": 'rtf:"Rich Text Format"',
    "txt": 'txt:Text (encoded):UTF8',
    "html": "html",
    "xlsx": 'xlsx:"Calc MS Excel 2007 XML"',
    "ods": "ods",
    "csv": 'csv:"Text - txt - csv (StarCalc)":44,34,76,1',
    "pptx": 'pptx:"Impress MS PowerPoint 2007 XML"',
    "odp": "odp",
}


def libreoffice_convert(src: Path, target: str, out_dir: Path) -> Path:
    soffice = find_libreoffice()
    if not soffice:
        raise ConversionError("LibreOffice isn't installed on the server, so office documents can't be converted.")
    job = Path(tempfile.mkdtemp(prefix="lo_", dir=str(out_dir)))
    # A private profile per job lets several conversions run at once.
    profile = Path(tempfile.mkdtemp(prefix="lo_profile_"))
    try:
        filt = LO_FILTERS[target].replace('"', "")
        cmd = [soffice, f"-env:UserInstallation={profile.as_uri()}", "--headless", "--norestore",
               "--convert-to", filt, "--outdir", str(job), str(src)]
        _run(cmd, OFFICE_TIMEOUT, "LibreOffice")
        produced = [p for p in job.iterdir() if p.is_file()]
        if not produced:
            raise ConversionError(f"LibreOffice couldn't convert {src.name} to {label(target)}.")
        out = _dest(src, out_dir, target)
        shutil.move(str(produced[0]), out)
        return out
    finally:
        shutil.rmtree(job, ignore_errors=True)
        shutil.rmtree(profile, ignore_errors=True)


PANDOC_READ = {"docx": "docx", "odt": "odt", "md": "markdown", "html": "html", "htm": "html", "rtf": "rtf", "epub": "epub"}
PANDOC_WRITE = {"docx": "docx", "odt": "odt", "md": "gfm", "html": "html", "rtf": "rtf", "txt": "plain", "epub": "epub"}


def pandoc_convert(src: Path, target: str, out_dir: Path) -> Path:
    out = _dest(src, out_dir, target)
    if out == src:
        out = out_dir / f"{src.stem}_converted.{target}"
    cmd = ["pandoc", "-f", PANDOC_READ[ext_of(src)], "-t", PANDOC_WRITE[target], "-o", str(out), str(src)]
    if target in ("html", "epub", "rtf"):
        cmd.insert(1, "--standalone")
        cmd += ["--metadata", f"title={src.stem}"]
    _run(cmd, OFFICE_TIMEOUT, "pandoc")
    return out


def _stage(src: Path, out_dir: Path) -> Path:
    """Directory for intermediate files, so they never collide with final outputs."""
    d = out_dir / f"_tmp_{uuid.uuid4().hex[:8]}"
    d.mkdir()
    return d


def convert_text_document(src: Path, target: str, out_dir: Path) -> list[Path]:
    s = ext_of(src)
    if s == "htm":
        s = "html"
    tmp = _stage(src, out_dir)

    if s == "txt" and target == "md":
        out = _dest(src, out_dir, "md")
        shutil.copyfile(src, out)
        return [out]

    if target == "pdf":
        if s in ("md", "html"):
            src = pandoc_convert(src, "docx", tmp)  # pandoc's PDF path needs LaTeX; LibreOffice doesn't
        return [libreoffice_convert(src, "pdf", out_dir)]

    if target in ("md", "epub") or s == "md" or (s == "html" and target != "txt"):
        if ext_of(src) not in PANDOC_READ:  # e.g. .doc or .txt: let LibreOffice make a .docx first
            src = libreoffice_convert(src, "docx", tmp)
        out = pandoc_convert(src, target, tmp)
        final = _dest(src, out_dir, target)
        shutil.move(str(out), final)
        return [final]

    return [libreoffice_convert(src, target, out_dir)]


def convert_office(src: Path, target: str, out_dir: Path) -> list[Path]:
    """Spreadsheets and presentations go straight through LibreOffice."""
    if target in ("png", "jpg"):  # presentation -> one image per slide
        pdf = libreoffice_convert(src, "pdf", _stage(src, out_dir))
        return pdf_to_images(pdf, target, out_dir, stem=src.stem)
    return [libreoffice_convert(src, target, out_dir)]


def pdf_to_images(src: Path, target: str, out_dir: Path, stem: str | None = None, dpi: int = 150) -> list[Path]:
    stem = stem or src.stem
    outs = []
    with pymupdf.open(str(src)) as doc:
        if doc.needs_pass:
            raise ConversionError(f"{src.name} is password-protected.")
        for i, page in enumerate(doc, start=1):
            pix = page.get_pixmap(dpi=dpi, alpha=False)
            name = f"{stem}.{target}" if len(doc) == 1 else f"{stem}_page_{i}.{target}"
            out = out_dir / name
            if target == "jpg":
                pix.save(str(out), jpg_quality=90)
            else:
                pix.save(str(out))
            outs.append(out)
    return outs


def convert_pdf(src: Path, target: str, out_dir: Path) -> list[Path]:
    try:
        doc = pymupdf.open(str(src))
    except Exception as exc:
        raise ConversionError(f"Can't open {src.name} as a PDF ({exc}).")
    if doc.needs_pass:
        doc.close()
        raise ConversionError(f"{src.name} is password-protected. Unlock it first.")
    if target == "txt":
        out = _dest(src, out_dir, "txt")
        text = "\n\n".join(page.get_text() for page in doc)
        doc.close()
        out.write_text(text, encoding="utf-8")
        return [out]
    doc.close()
    if target in ("png", "jpg"):
        return pdf_to_images(src, target, out_dir)
    if target == "docx":
        from pdf2docx import Converter
        out = _dest(src, out_dir, "docx")
        cv = Converter(str(src))
        try:
            cv.convert(str(out))
        finally:
            cv.close()
        return [out]
    raise ConversionError(f"Can't convert PDF to {label(target)}.")


# ---------- Router ----------
def convert_file(src: Path, target: str, out_dir: Path, options: dict | None = None, category: str | None = None) -> list[Path]:
    options = options or {}
    target = target.lower().lstrip(".")
    if target == "jpeg":
        target = "jpg"
    if target == "tif":
        target = "tiff"
    ext = ext_of(src)
    cat, targets = resolve(ext, target, category)
    if cat is None:
        if category and ext in REGISTRY:
            raise ConversionError(f"{src.name}: .{ext} files don't belong in the {category} converter.")
        raise ConversionError(f"{src.name}: .{ext or '?'} files aren't supported yet.")
    if target not in targets:
        raise ConversionError(f"{src.name}: can't convert {label(ext)} to {label(target)}.")
    validate_file(src)

    if cat == "image":
        return convert_image(src, target, out_dir, quality=options.get("quality", 90), max_size=options.get("max_size"))
    if cat in ("audio", "video"):
        return convert_media(src, target, out_dir, audio_bitrate=options.get("audio_bitrate", "192k"), max_height=options.get("max_height"))
    if cat == "document":
        return convert_text_document(src, target, out_dir)
    if cat in ("spreadsheet", "presentation"):
        return convert_office(src, target, out_dir)
    if cat == "pdf":
        return convert_pdf(src, target, out_dir)
    if cat == "data":
        return data_converter.convert_data(src, target, out_dir)
    if cat == "archive":
        return archive_converter.convert_archive(src, target, out_dir)
    if cat == "font":
        return font_converter.convert_font(src, target, out_dir)
    if cat == "model":
        return model_converter.convert_model(src, target, out_dir)
    raise ConversionError("Unsupported file type.")


def zip_outputs(paths: list[Path], out_dir: Path, name: str = "converted.zip") -> Path:
    archive = out_dir / name
    seen = set()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in paths:
            arc = p.name
            n = 1
            while arc in seen:  # two inputs with the same stem
                arc = f"{p.stem}_{n}{p.suffix}"
                n += 1
            seen.add(arc)
            zf.write(p, arcname=arc)
    return archive
