import os
import shutil
import tempfile
from pathlib import Path

from flask import Flask, request, jsonify, send_file
from dotenv import load_dotenv

from converter import (
    CATEGORIES, ConversionError, REGISTRY, UPLOAD_DIR, cleanup_old_jobs, convert_file,
    ext_of, formats_payload, images_to_single_pdf, safe_name, zip_outputs,
)

load_dotenv()
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_UPLOAD_MB", "500")) * 1024 * 1024
MAX_FILES = 20

PROJECT_ROOT = Path(__file__).resolve().parent


@app.route("/")
def index():
    return send_file(PROJECT_ROOT / "index.html", mimetype="text/html")


@app.get("/health")
def health():
    return jsonify({"ok": True})


@app.get("/api/formats")
def formats():
    """Everything the UI needs to build the format picker, plus the upload limits."""
    payload = formats_payload()
    payload["limits"] = {
        "max_upload_mb": app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024),
        "max_files": MAX_FILES,
    }
    return jsonify(payload)


def _int_option(name, default=None, lo=None, hi=None):
    raw = (request.form.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    if lo is not None:
        value = max(lo, value)
    if hi is not None:
        value = min(hi, value)
    return value


@app.post("/api/convert")
def convert():
    target = (request.form.get("target") or "").strip().lower().lstrip(".")
    uploads = [u for u in request.files.getlist("files") if u and u.filename]
    if not uploads:
        return jsonify({"error": "Add at least one file."}), 400
    if len(uploads) > MAX_FILES:
        return jsonify({"error": f"You can convert up to {MAX_FILES} files at once."}), 400
    if not target:
        return jsonify({"error": "Pick a format to convert to."}), 400

    bitrate = request.form.get("audio_bitrate", "192k")
    options = {
        "quality": _int_option("quality", 90, 1, 100),
        "max_size": _int_option("max_size", None, 16, 20000),
        "max_height": _int_option("max_height", None, 144, 4320),
        "audio_bitrate": bitrate if bitrate in {"96k", "128k", "192k", "256k", "320k"} else "192k",
    }
    combine = request.form.get("combine_pdf", "false").lower() == "true"

    cleanup_old_jobs()
    work_dir = Path(tempfile.mkdtemp(prefix="job_", dir=str(UPLOAD_DIR)))
    in_dir, out_dir = work_dir / "in", work_dir / "out"
    in_dir.mkdir()
    out_dir.mkdir()

    try:
        sources = []
        for i, upload in enumerate(uploads):
            name = safe_name(upload.filename)
            path = in_dir / f"{i}" / name  # subfolder per file keeps duplicate names apart
            path.parent.mkdir()
            upload.save(path)
            sources.append(path)

        category = (request.form.get("category") or "").strip().lower()
        if category:
            for src in sources:
                if ext_of(src) not in CATEGORIES.get(category, {}):
                    kind = {"model": "3D model"}.get(category, category)
                    raise ConversionError(f"{src.name} isn't {'an' if kind[0] in 'aeiou' else 'a'} {kind} file. Use the matching converter tab.")

        all_images = all(REGISTRY.get(ext_of(s), {}).get("category") == "image" for s in sources)
        if combine and target == "pdf" and all_images and len(sources) > 1:
            outputs = [images_to_single_pdf(sources, out_dir)]
        else:
            outputs = []
            for src in sources:
                outputs.extend(convert_file(src, target, out_dir, options, category=category or None))

        if len(outputs) == 1:
            result = outputs[0]
        else:
            stem = sources[0].stem if len(sources) == 1 else "converted"
            result = zip_outputs(outputs, work_dir, f"{stem}_{target}.zip")

        response = send_file(result, as_attachment=True, download_name=result.name)
        response.call_on_close(lambda: shutil.rmtree(work_dir, ignore_errors=True))
        return response
    except ConversionError as exc:
        shutil.rmtree(work_dir, ignore_errors=True)
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:  # unexpected: keep the message, drop the traceback
        app.logger.exception("conversion failed")
        shutil.rmtree(work_dir, ignore_errors=True)
        return jsonify({"error": f"Conversion failed: {exc}"}), 500


@app.errorhandler(413)
def too_large(_):
    mb = app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024)
    return jsonify({"error": f"Files are too large. The limit is {mb} MB per upload."}), 413


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=os.environ.get("FLASK_DEBUG") == "1")
