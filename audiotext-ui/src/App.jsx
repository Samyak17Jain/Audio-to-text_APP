import { useEffect, useRef, useState } from "react";
import "./index.css";

const MAX_BYTES = 12 * 1024 * 1024;

function prettyBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
}

export default function App() {
  const [email, setEmail] = useState("");
  const [file, setFile] = useState(null);
  const [drag, setDrag] = useState(false);
  const [status, setStatus] = useState("");
  const [sending, setSending] = useState(false);
  const [progress, setProgress] = useState(0);
  const [history, setHistory] = useState([]);

  const fileRef = useRef();

  useEffect(() => {
    const saved = localStorage.getItem("audio_uploads");
    if (saved) setHistory(JSON.parse(saved));
  }, []);

  useEffect(() => {
    localStorage.setItem("audio_uploads", JSON.stringify(history));
  }, [history]);

  function validateFile(f) {
    if (f.size > MAX_BYTES) {
      setStatus(`❌ File too large (${prettyBytes(f.size)}). Max 12MB.`);
      return false;
    }
    return true;
  }

  function handleFile(f) {
    if (!f) return;
    if (!validateFile(f)) return;
    setFile(f);
    setStatus("");
  }

  async function handleSubmit(e) {
    e.preventDefault();
    if (!email.includes("@")) {
      setStatus("❌ Please enter a valid email address");
      return;
    }
    if (!file) {
      setStatus("❌ Please select an audio file");
      return;
    }

    setSending(true);
    setProgress(0);
    setStatus("Uploading audio…");

    const fd = new FormData();
    fd.append("email", email);
    fd.append("audio", file);

    try {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/submit");

      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) {
          setProgress(Math.round((e.loaded / e.total) * 100));
        }
      };

      xhr.onload = () => {
        const res = JSON.parse(xhr.responseText || "{}");
        const jobId = res.job_id || crypto.randomUUID().slice(0, 8);

        setHistory((h) => [
          {
            id: jobId,
            name: file.name,
            time: new Date().toLocaleString(),
            email,
          },
          ...h,
        ]);

        setStatus(
          `✅ Audio received successfully!\nYou’ll get the transcription at ${email}.`
        );
        setFile(null);
        setSending(false);
        setProgress(0);
      };

      xhr.onerror = () => {
        setStatus("❌ Network error. Please try again.");
        setSending(false);
      };

      xhr.send(fd);
    } catch {
      setStatus("❌ Something went wrong.");
      setSending(false);
    }
  }

  return (
    <div className="container">
      <div className="card">
        <h1>🎧 Audio → Transcription</h1>
        <p className="muted">
          Upload an audio file and receive the transcription securely by email.
        </p>

        <form onSubmit={handleSubmit}>
          <label className="label">Step 1 · Your email</label>
          <input
            className="input"
            type="email"
            placeholder="you@example.com"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />

          <label className="label">Step 2 · Upload audio</label>
          <div
            className={`dropzone ${drag ? "drag" : ""}`}
            onDragOver={(e) => {
              e.preventDefault();
              setDrag(true);
            }}
            onDragLeave={() => setDrag(false)}
            onDrop={(e) => {
              e.preventDefault();
              setDrag(false);
              handleFile(e.dataTransfer.files[0]);
            }}
          >
            <div>
              <strong>{file ? file.name : "Drag & drop audio file here"}</strong>
              <div className="muted">
                {file
                  ? prettyBytes(file.size)
                  : "mp3 · wav · m4a (max 12MB)"}
              </div>
            </div>
            <div>
              <input
                ref={fileRef}
                className="file-input"
                type="file"
                accept="audio/*"
                onChange={(e) => handleFile(e.target.files[0])}
              />
              <button
                type="button"
                className="button small"
                onClick={() => fileRef.current.click()}
              >
                Choose file
              </button>
            </div>
          </div>

          <label className="label">Step 3 · Transcribe</label>
          <button className="button" disabled={sending}>
            {sending ? "Processing…" : "Submit"}
          </button>
        </form>

        {sending && (
          <div className="progress-wrap">
            <div className="progress" style={{ width: `${progress}%` }}>
              {progress}%
            </div>
          </div>
        )}

        <div className={`status ${status.startsWith("❌") ? "err" : ""}`}>
          {status || "Status updates will appear here."}
        </div>
      </div>

      <div className="card small">
        <h3>Recent uploads</h3>
        {history.length === 0 ? (
          <p className="muted">No uploads yet.</p>
        ) : (
          <ul className="history">
            {history.map((h) => (
              <li key={h.id}>
                <div className="row">
                  <div>
                    <div className="item-title">{h.name}</div>
                    <div className="muted">{h.time}</div>
                  </div>
                  <div className="jobid">{h.id}</div>
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
