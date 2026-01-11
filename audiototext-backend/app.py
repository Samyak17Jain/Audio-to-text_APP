# app.py
# Single-file server that reuses your transcription worker logic and exposes a FastAPI API.
# Run: python app.py
# NOTE: set SMTP_USERNAME and SMTP_PASSWORD as env vars if you want email sending.

import os
import time
import tempfile
import uuid
import threading
import queue
import traceback
from email.message import EmailMessage

# OPTIONAL: small Windows asyncio fix if you use Windows and uvicorn
import sys
if sys.platform == "win32":
    try:
        import asyncio
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass

# --- third-party imports ---
from typing import Optional
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import JSONResponse
import uvicorn

# Audio / model deps (make sure these are installed)
import whisper
import torch
from pydub import AudioSegment
import smtplib

# ---------------- CONFIG ----------------
MODEL_NAME = os.getenv("MODEL_NAME", "tiny")  # change to "small" etc if desired
SEG_LEN_SECONDS = int(os.getenv("SEG_LEN_SECONDS", "20"))
CHUNK_THRESHOLD_S = int(os.getenv("CHUNK_THRESHOLD_S", "40"))
MAX_UPLOAD_S = int(os.getenv("MAX_UPLOAD_S", "600"))
PORT = int(os.getenv("PORT", "8000"))

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")

EMAIL_FROM = SMTP_USERNAME if SMTP_USERNAME else "no-reply@example.com"
EMAIL_SUBJECT_TEMPLATE = "Your transcription (job {job_id})"
# ------------------------------------------------

# ---------------- job queue & worker ----------------
job_q = queue.Queue()
WORKER_THREAD = None
SHUTDOWN_EVENT = threading.Event()

def get_device():
    return "cuda" if torch.cuda.is_available() else "cpu"

print(f"[app] Loading Whisper model '{MODEL_NAME}' (this may download weights)...")
device = get_device()
model = whisper.load_model(MODEL_NAME, device=device)
print(f"[app] Model '{MODEL_NAME}' loaded on {device}.")

# ---------- audio helpers ----------
def ensure_wav_copy(in_path):
    """
    Convert input audio to WAV file in tempdir and return path + duration_s.
    """
    tmpdir = tempfile.gettempdir()
    out_path = os.path.join(tmpdir, f"whisper_input_{uuid.uuid4().hex}.wav")
    audio = AudioSegment.from_file(in_path)
    audio.export(out_path, format="wav")
    return out_path, len(audio) / 1000.0

def split_audio_to_files(audio_path, seg_len_s=SEG_LEN_SECONDS):
    audio = AudioSegment.from_file(audio_path)
    duration_ms = len(audio)
    seg_len_ms = int(seg_len_s * 1000)
    out_files = []
    base_ts = int(time.time())
    for i, start_ms in enumerate(range(0, duration_ms, seg_len_ms)):
        end_ms = min(start_ms + seg_len_ms, duration_ms)
        chunk = audio[start_ms:end_ms]
        out_path = os.path.join(tempfile.gettempdir(), f"whisper_chunk_{base_ts}_{uuid.uuid4().hex}_{i}.wav")
        chunk.export(out_path, format="wav")
        out_files.append(out_path)
    return out_files

def transcribe_chunk(path):
    res = model.transcribe(path)
    return res.get("text", "").strip()

def transcribe_file(audio_path):
    """
    Transcribe audio_path (auto-chunk if long). Returns the transcript string.
    """
    wav_path, duration_s = ensure_wav_copy(audio_path)
    try:
        if duration_s > CHUNK_THRESHOLD_S:
            parts = []
            out_paths = split_audio_to_files(wav_path, seg_len_s=SEG_LEN_SECONDS)
            for idx, p in enumerate(out_paths, start=1):
                try:
                    txt = transcribe_chunk(p)
                except Exception as e:
                    txt = f"[ERROR transcribing chunk {idx}: {e}]"
                parts.append(txt)
                try:
                    os.remove(p)
                except Exception:
                    pass
            joined = "\n\n".join([p for p in parts if p])
            summary = f"(Chunked — {len(out_paths)} chunks, original duration {duration_s:.1f}s)"
            return summary + "\n\n" + joined
        else:
            res = model.transcribe(wav_path)
            text = res.get("text", "").strip()
            summary = f"(Single-pass — duration {duration_s:.1f}s)"
            return summary + "\n\n" + text
    finally:
        try:
            if wav_path and os.path.exists(wav_path):
                os.remove(wav_path)
        except Exception:
            pass

# -------- email helper (with fallback save) --------
def send_email_with_fallback(to_address: str, subject: str, body_text: str,
                             attachments: list = None, fallback_save_path: str = None):
    """
    attachments: list of tuples (filename, bytes, mime_type)
    """
    msg = EmailMessage()
    msg["From"] = EMAIL_FROM
    msg["To"] = to_address
    msg["Subject"] = subject
    msg.set_content(body_text)

    if attachments:
        for fname, data_bytes, mime in attachments:
            maintype, subtype = mime.split("/", 1)
            msg.add_attachment(data_bytes, maintype=maintype, subtype=subtype, filename=fname)

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
            smtp.send_message(msg)
        print(f"[email] Sent to {to_address}")
        return True, None
    except Exception as e:
        err = f"SMTP send error: {e}\n{traceback.format_exc()}"

    print("[email] Send failed:", err)
    if fallback_save_path:
        try:
            if attachments and attachments[0] and attachments[0][1]:
                with open(fallback_save_path, "wb") as f:
                    f.write(attachments[0][1])
            else:
                with open(fallback_save_path, "wb") as f:
                    f.write(body_text.encode("utf-8"))
            print(f"[email] Saved fallback transcript at: {fallback_save_path}")
        except Exception as e:
            print("[email] Fallback save failed:", e)
    return False, err

# ---------- worker ----------
def worker_loop():
    print("[worker] Background worker started, waiting for jobs...")
    while not SHUTDOWN_EVENT.is_set():
        try:
            job = job_q.get(timeout=1.0)
        except queue.Empty:
            continue
        try:
            job_id = job.get("job_id")
            email = job.get("email")
            audio_path = job.get("audio_path")
            original_filename = job.get("filename", "upload")

            print(f"[worker] Processing job {job_id} (file={original_filename}, email={email})")

            try:
                transcript = transcribe_file(audio_path)
            except Exception as e:
                transcript = f"[ERROR] Transcription failed: {e}\n\n{traceback.format_exc()}"
                print(f"[worker] Transcription error for job {job_id}: {e}")

            subject = EMAIL_SUBJECT_TEMPLATE.format(job_id=job_id)
            body = f"Hello,\n\nAttached is the transcription for job {job_id} (file: {original_filename}).\n\n--- Transcript below ---\n\n{transcript}\n\nRegards,\nYour Transcription Server"

            fallback_path = os.path.join(tempfile.gettempdir(), f"transcript_fallback_{job_id}.txt")
            ok, err = send_email_with_fallback(
                email,
                subject,
                body,
                attachments=[(f"transcript_{job_id}.txt", transcript.encode("utf-8"), "text/plain")],
                fallback_save_path=fallback_path
            )
            if not ok:
                print(f"[worker] Email failed for job {job_id}. Saved fallback at: {fallback_path}. Error: {err}")
            else:
                print(f"[worker] Email successfully sent for job {job_id} -> {email}")

            # cleanup uploaded audio file
            try:
                if os.path.exists(audio_path):
                    os.remove(audio_path)
            except Exception:
                pass

        finally:
            job_q.task_done()

def start_worker():
    global WORKER_THREAD
    if WORKER_THREAD is None or not WORKER_THREAD.is_alive():
        WORKER_THREAD = threading.Thread(target=worker_loop, daemon=True, name="transcribe-worker")
        WORKER_THREAD.start()

# ---------- FastAPI app ----------
app = FastAPI(title="Audio Transcription API")

@app.post("/api/submit")
async def submit(email: str = Form(...), audio: UploadFile = File(...)):
    # basic validation
    if "@" not in email:
        return JSONResponse({"error": "Invalid email"}, status_code=400)

    # save file to temp
    suffix = os.path.splitext(audio.filename)[1] or ".wav"
    dest = os.path.join(tempfile.gettempdir(), f"upload_{uuid.uuid4().hex}{suffix}")
    with open(dest, "wb") as f:
        f.write(await audio.read())

    # quick duration guard
    try:
        a = AudioSegment.from_file(dest)
        duration_s = len(a) / 1000.0
        if duration_s > MAX_UPLOAD_S:
            try:
                os.remove(dest)
            except:
                pass
            return JSONResponse({"error": f"Uploaded audio too long ({duration_s:.1f}s). Max {MAX_UPLOAD_S}s."}, status_code=400)
    except Exception:
        duration_s = None

    job_id = uuid.uuid4().hex[:12]
    job = {
        "job_id": job_id,
        "email": email,
        "audio_path": dest,
        "filename": audio.filename,
        "submitted_at": time.time()
    }
    job_q.put(job)
    start_worker()

    return {"message": f"Job accepted (ID: {job_id}). Check email (or fallback file).", "job_id": job_id}

@app.get("/api/health")
def health():
    return {"status": "ok"}

# ------------- shutdown handler -------------
import atexit
def shutdown():
    SHUTDOWN_EVENT.set()
    if WORKER_THREAD:
        WORKER_THREAD.join(timeout=2.0)
atexit.register(shutdown)

# ------------- run server -------------
if __name__ == "__main__":
    print(f"[app] Starting API on port {PORT} ...")
    # Use uvicorn programmatically to make it easy to run with python app.py
    uvicorn.run("app:app", host="127.0.0.1", port=PORT, log_level="info", reload=False)
