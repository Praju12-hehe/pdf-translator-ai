import os
import time
import uuid
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
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
MAX_CONTENT_LENGTH = 200 * 1024 * 1024
LANG_MAP = {"hindi": "hi", "marathi": "mr"}

TRANSLATION_WORKERS = 8
MAX_TRANSLATE_RETRIES = 5
INITIAL_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 16.0

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


def _extract_block_text(block):
    """Concatenate all spans in a block into one paragraph; return (text, avg_size)."""
    line_strings = []
    sizes = []
    for line in block.get("lines", []):
        parts = []
        for span in line.get("spans", []):
            t = span.get("text", "")
            if t:
                parts.append(t)
                size = span.get("size")
                if size:
                    sizes.append(float(size))
        if parts:
            line_strings.append("".join(parts).strip())
    text = " ".join(s for s in line_strings if s).strip()
    avg_size = (sum(sizes) / len(sizes)) if sizes else 11.0
    return text, avg_size


def _translate_pdf(job_id: str, input_path: Path, output_path: Path, lang_code: str):
    try:
        _set_progress(job_id, status="processing", progress=2, message="Opening PDF")

        doc = fitz.open(str(input_path))
        total_pages = len(doc)
        if total_pages == 0:
            raise RuntimeError("The uploaded PDF has no pages.")

        # ---------- Pass 1: extract block-level text from every page ----------
        pages_blocks = []  # list per page of [(text, bbox, size)]
        unique_texts = set()
        for page_index, page in enumerate(doc):
            page_blocks = []
            for block in page.get_text("dict").get("blocks", []):
                if block.get("type", 0) != 0:
                    continue
                text, avg_size = _extract_block_text(block)
                if not text:
                    continue
                bbox = fitz.Rect(block["bbox"])
                page_blocks.append((text, bbox, avg_size))
                unique_texts.add(text)
            pages_blocks.append(page_blocks)

            if total_pages >= 50 and (page_index + 1) % 25 == 0:
                _set_progress(
                    job_id,
                    progress=2 + int((page_index + 1) / total_pages * 3),
                    message=f"Scanned {page_index + 1}/{total_pages} pages",
                )

        unique_list = list(unique_texts)
        total_unique = len(unique_list)
        _set_progress(
            job_id,
            progress=5,
            message=(
                f"Extracted {sum(len(p) for p in pages_blocks)} blocks, "
                f"{total_unique} unique paragraphs"
            ),
        )

        # ---------- Pass 2: translate unique texts in parallel with caching ----------
        translation_cache = {}
        cache_lock = threading.Lock()
        tls = threading.local()

        def _get_translator():
            t = getattr(tls, "translator", None)
            if t is None:
                # One translator per worker thread = one persistent httpx.Client
                # per thread, providing connection pooling for that worker.
                t = Translator()
                tls.translator = t
            return t

        def _translate_one(text: str) -> str:
            with cache_lock:
                cached = translation_cache.get(text)
            if cached is not None:
                return cached

            backoff = INITIAL_BACKOFF_SECONDS
            last_error = None
            for attempt in range(MAX_TRANSLATE_RETRIES):
                try:
                    translator = _get_translator()
                    result = translator.translate(text, dest=lang_code)
                    translated = (
                        result.text if result and result.text else text
                    )
                    with cache_lock:
                        translation_cache[text] = translated
                    return translated
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    msg = str(exc).lower()
                    rate_limited = (
                        "429" in msg
                        or "too many" in msg
                        or "rate" in msg
                        or "quota" in msg
                    )
                    if attempt == MAX_TRANSLATE_RETRIES - 1:
                        break
                    sleep_for = backoff if rate_limited else min(backoff, 2.0)
                    time.sleep(sleep_for)
                    backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
                    # Reset translator on hard failures so a fresh client is used
                    if not rate_limited:
                        try:
                            tls.translator = Translator()
                        except Exception:
                            pass

            # All retries exhausted — keep original text but cache it so we
            # don't waste more attempts on the same string.
            if last_error is not None:
                print(f"[translate] giving up on text after retries: {last_error}")
            with cache_lock:
                translation_cache[text] = text
            return text

        completed = 0
        completed_lock = threading.Lock()

        def _task(text):
            nonlocal completed
            translated = _translate_one(text)
            with completed_lock:
                completed += 1
                done = completed
            # Translation phase occupies 5% -> 70%
            if total_unique:
                pct = 5 + int(done / total_unique * 65)
                _set_progress(
                    job_id,
                    progress=pct,
                    message=f"Translated {done}/{total_unique} paragraphs",
                )
            return translated

        if unique_list:
            with ThreadPoolExecutor(max_workers=TRANSLATION_WORKERS) as pool:
                futures = [pool.submit(_task, t) for t in unique_list]
                for fut in as_completed(futures):
                    fut.result()

        # ---------- Pass 3: redact + write translations back, page by page ----------
        font_file_str = str(FONT_PATH)
        for page_index, page in enumerate(doc):
            page_blocks = pages_blocks[page_index]
            if page_blocks:
                for _, bbox, _ in page_blocks:
                    page.add_redact_annot(bbox, fill=(1, 1, 1))
                page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)

                for original, bbox, size in page_blocks:
                    translated = translation_cache.get(original, original)
                    if not translated:
                        continue
                    inserted = False
                    for scale in (1.0, 0.88, 0.75, 0.62, 0.5):
                        try_size = max(6.0, size * scale)
                        rect = fitz.Rect(
                            bbox.x0,
                            bbox.y0,
                            bbox.x1 + 4,
                            bbox.y1 + 8,
                        )
                        rc = page.insert_textbox(
                            rect,
                            translated,
                            fontname="NotoDev",
                            fontfile=font_file_str,
                            fontsize=try_size,
                            color=(0, 0, 0),
                            align=0,
                        )
                        if rc >= 0:
                            inserted = True
                            break
                    if not inserted:
                        page.insert_textbox(
                            fitz.Rect(
                                bbox.x0,
                                bbox.y0,
                                bbox.x1 + 80,
                                bbox.y1 + 40,
                            ),
                            translated,
                            fontname="NotoDev",
                            fontfile=font_file_str,
                            fontsize=max(6.0, size * 0.45),
                            color=(0, 0, 0),
                            align=0,
                        )

            # Write phase occupies 70% -> 95%, reported as pages completed
            pct = 70 + int((page_index + 1) / total_pages * 25)
            _set_progress(
                job_id,
                progress=pct,
                pages_done=page_index + 1,
                pages_total=total_pages,
                message=f"Rendered {page_index + 1}/{total_pages} pages",
            )

        _set_progress(job_id, progress=96, message="Saving translated PDF")
        doc.save(str(output_path), deflate=True, garbage=3)
        doc.close()

        _set_progress(
            job_id,
            status="done",
            progress=100,
            message=(
                f"Translated {total_pages} pages — "
                f"{total_unique} unique paragraphs, "
                f"{len(translation_cache)} cached"
            ),
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
            "pages_done": 0,
            "pages_total": 0,
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
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
