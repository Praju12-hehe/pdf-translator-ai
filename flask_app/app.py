import os
import uuid
import threading
import traceback
from pathlib import Path

import fitz
from flask import (
    Flask,
    render_template,
    request,
    jsonify,
    send_from_directory,
    abort,
)
from googletrans import Translator
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "outputs"
FONT_PATH = BASE_DIR / "fonts" / "NotoSansDevanagari.ttf"
ALLOWED_EXTENSIONS = {"pdf"}
MAX_CONTENT_LENGTH = 25 * 1024 * 1024
LANG_MAP = {"hindi": "hi", "marathi": "mr"}

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH

JOBS = {}
JOBS_LOCK = threading.Lock()


def _allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def _set_progress(job_id: str, **fields):
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(fields)


def _translate_pdf(job_id: str, input_path: Path, output_path: Path, lang_code: str):
    try:
        _set_progress(job_id, status="processing", progress=2, message="Opening PDF")

        translator = Translator()
        doc = fitz.open(str(input_path))
        total_pages = len(doc)

        if total_pages == 0:
            raise RuntimeError("The uploaded PDF has no pages.")

        for page_index, page in enumerate(doc):
            page_label = f"page {page_index + 1} of {total_pages}"
            _set_progress(
                job_id,
                progress=int(5 + (page_index / total_pages) * 85),
                message=f"Translating {page_label}",
            )

            text_dict = page.get_text("dict")
            spans_to_replace = []

            for block in text_dict.get("blocks", []):
                if block.get("type", 0) != 0:
                    continue
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        original = (span.get("text") or "").strip()
                        if not original:
                            continue
                        bbox = fitz.Rect(span["bbox"])
                        size = float(span.get("size", 11)) or 11.0
                        spans_to_replace.append((original, bbox, size))

            translated_pairs = []
            for original, bbox, size in spans_to_replace:
                try:
                    result = translator.translate(original, dest=lang_code)
                    translated_text = (
                        result.text if result and result.text else original
                    )
                except Exception:
                    translated_text = original
                translated_pairs.append((translated_text, bbox, size))

            for _, bbox, _ in spans_to_replace:
                page.add_redact_annot(bbox, fill=(1, 1, 1))
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)

            page.insert_font(fontname="NotoDev", fontfile=str(FONT_PATH))

            for translated_text, bbox, size in translated_pairs:
                if not translated_text:
                    continue
                rect = fitz.Rect(
                    bbox.x0,
                    bbox.y0 - 1,
                    bbox.x1 + 50,
                    bbox.y1 + 6,
                )
                rc = page.insert_textbox(
                    rect,
                    translated_text,
                    fontname="NotoDev",
                    fontsize=size,
                    color=(0, 0, 0),
                    align=0,
                )
                if rc < 0:
                    shrunk_size = max(6.0, size * 0.75)
                    page.insert_textbox(
                        fitz.Rect(
                            bbox.x0,
                            bbox.y0 - 1,
                            bbox.x1 + 120,
                            bbox.y1 + 14,
                        ),
                        translated_text,
                        fontname="NotoDev",
                        fontsize=shrunk_size,
                        color=(0, 0, 0),
                        align=0,
                    )

        _set_progress(job_id, progress=92, message="Saving translated PDF")
        doc.save(str(output_path), deflate=True, garbage=3)
        doc.close()

        _set_progress(
            job_id,
            status="done",
            progress=100,
            message="Translation complete",
            output=output_path.name,
        )
    except Exception as exc:
        traceback.print_exc()
        _set_progress(
            job_id,
            status="error",
            progress=100,
            message=f"Failed: {exc}",
        )
    finally:
        try:
            input_path.unlink(missing_ok=True)
        except Exception:
            pass


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/translate", methods=["POST"])
def translate_endpoint():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    file = request.files["file"]
    language = (request.form.get("language") or "").lower()

    if file.filename == "":
        return jsonify({"error": "No file selected"}), 400
    if not _allowed_file(file.filename):
        return jsonify({"error": "Only .pdf files are accepted"}), 400
    if language not in LANG_MAP:
        return jsonify({"error": "Choose Hindi or Marathi"}), 400

    job_id = uuid.uuid4().hex
    safe_name = secure_filename(file.filename) or "document.pdf"
    input_path = UPLOAD_DIR / f"{job_id}__{safe_name}"
    output_name = f"{job_id}__translated_{language}.pdf"
    output_path = OUTPUT_DIR / output_name
    file.save(str(input_path))

    with JOBS_LOCK:
        JOBS[job_id] = {
            "status": "queued",
            "progress": 0,
            "message": "Queued",
            "language": language,
            "original_name": safe_name,
            "output": None,
        }

    thread = threading.Thread(
        target=_translate_pdf,
        args=(job_id, input_path, output_path, LANG_MAP[language]),
        daemon=True,
    )
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/progress/<job_id>")
def progress(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "Unknown job"}), 404
        return jsonify(job)


@app.route("/download/<job_id>")
def download(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        abort(404)
    if job["status"] != "done" or not job.get("output"):
        abort(409)

    download_name = (
        Path(job["original_name"]).stem
        + f"_{job['language']}.pdf"
    )
    return send_from_directory(
        OUTPUT_DIR,
        job["output"],
        as_attachment=True,
        download_name=download_name,
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
