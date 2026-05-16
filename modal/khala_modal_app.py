"""Modal serverless wrapper for Khala music generation.

This app intentionally runs Khala as an on-demand GPU function rather than a
persistent public endpoint by default. The GPU container exits after each job,
so Modal should scale back to zero automatically.

Usage from repo root:

    modal run modal/khala_modal_app.py::generate_cli \
      --prompt "High-energy futuristic rave opener..." \
      --lyrics-file /path/to/lyrics.txt \
      --duration 1 \
      --out /tmp/khala_sample.mp3

Optional web endpoint:

    modal deploy modal/khala_modal_app.py

Then POST JSON to /generate. Keep endpoint scale-to-zero and check Modal tasks
when finished.
"""

from __future__ import annotations

import json
import os
import pathlib
import signal
import subprocess
import time
import urllib.request
from typing import Optional

import modal

APP_NAME = "khala-music-modal"
MODEL_REPO = "liujiafeng/Khala-MusicGeneration-v1.0"
REPO_DIR = pathlib.Path("/root/Khala")
BACKEND_DIR = REPO_DIR / "backend"
CHECKPOINTS_DIR = REPO_DIR / "checkpoints"
OUTPUTS_DIR = pathlib.Path("/outputs")
API_PORT = 8889

# The upstream prebuilt image already contains NGC PyTorch + Node + most runtime
# dependencies. We install lightweight Python/API helpers on top.
image = (
    modal.Image.from_registry("ghcr.io/davidliujiafeng/khala-env:ngc25.02-node24")
    .apt_install("ffmpeg", "git", "curl")
    .pip_install("huggingface_hub[hf_transfer]", "requests")
    .env({"HF_HUB_ENABLE_HF_TRANSFER": "1", "PYTHONUNBUFFERED": "1"})
    # Keep local files last: Modal disallows later image build steps after add_local_*.
    .add_local_dir(".", remote_path=str(REPO_DIR), ignore=[".git", "checkpoints", "backend/generated_audio", "frontend/node_modules"])
)

app = modal.App(APP_NAME, image=image)
checkpoints = modal.Volume.from_name("khala-checkpoints", create_if_missing=True)
outputs = modal.Volume.from_name("khala-outputs", create_if_missing=True)


def _http_json(path: str, payload: Optional[dict] = None, timeout: int = 30) -> dict:
    url = f"http://127.0.0.1:{API_PORT}{path}"
    if payload is None:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _http_bytes(path: str, timeout: int = 120) -> bytes:
    with urllib.request.urlopen(f"http://127.0.0.1:{API_PORT}{path}", timeout=timeout) as r:
        return r.read()


def _ensure_checkpoints() -> None:
    CHECKPOINTS_DIR.mkdir(parents=True, exist_ok=True)
    marker = CHECKPOINTS_DIR / ".khala_hf_download_complete"
    if marker.exists():
        print("[khala-modal] checkpoints already present")
        return
    print(f"[khala-modal] downloading {MODEL_REPO} to {CHECKPOINTS_DIR}")
    # Use the Python API instead of the `hf` CLI because some NGC images carry
    # an older Typer that can break the CLI entrypoint.
    from huggingface_hub import snapshot_download

    snapshot_download(
        repo_id=MODEL_REPO,
        local_dir=str(CHECKPOINTS_DIR),
    )
    marker.write_text(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + "\n")
    checkpoints.commit()


def _start_backend() -> subprocess.Popen:
    print("[khala-modal] starting backend")
    proc = subprocess.Popen(
        ["bash", "run_backend.sh", "--gpus", "0", "--runtime-mode", "one_shot"],
        cwd=str(BACKEND_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        preexec_fn=os.setsid,
    )
    deadline = time.time() + 900
    last_status = None
    while time.time() < deadline:
        if proc.poll() is not None:
            out = proc.stdout.read() if proc.stdout else ""
            raise RuntimeError(f"backend exited early with code {proc.returncode}: {out[-4000:]}")
        try:
            status = _http_json("/status", timeout=5)
            last_status = status
            if status.get("idle_gpus", 0) >= 1:
                print("[khala-modal] backend ready", status)
                return proc
            print("[khala-modal] waiting for worker", status)
        except Exception as exc:
            print("[khala-modal] backend not ready", repr(exc))
        time.sleep(10)
    raise TimeoutError(f"backend did not become ready; last_status={last_status}")


def _stop_backend(proc: Optional[subprocess.Popen]) -> None:
    if not proc:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=30)
    except Exception:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            pass


def _generate_once(prompt: str, lyrics: str, duration: int, mode: str, prompt_mode: str, top_k_bb: int, temperature: float) -> pathlib.Path:
    _ensure_checkpoints()
    proc: Optional[subprocess.Popen] = None
    try:
        proc = _start_backend()
        payload = {
            "mode": mode,
            "prompt_mode": prompt_mode,
            "prompt": prompt,
            "tags": prompt if prompt_mode == "tags" else "",
            "lyrics": lyrics,
            "duration": duration,
            "top_k_bb": top_k_bb,
            "temperature": temperature,
        }
        print("[khala-modal] submit", json.dumps({**payload, "lyrics": lyrics[:120] + ("..." if len(lyrics) > 120 else "")}, ensure_ascii=False))
        accepted = _http_json("/generate", payload, timeout=60)
        job_id = accepted["job_id"]
        deadline = time.time() + 3600
        last = None
        while time.time() < deadline:
            last = _http_json(f"/job/{job_id}", timeout=20)
            print("[khala-modal] poll", json.dumps(last)[:1000])
            if last.get("status") in {"completed", "partial"}:
                break
            time.sleep(15)
        else:
            raise TimeoutError(f"generation timed out; last={last}")
        if last.get("status") not in {"completed", "partial"}:
            raise RuntimeError(f"generation failed/unfinished: {last}")
        track_idx = next((t["index"] for t in last.get("tracks", []) if t.get("status") == "done"), None)
        if track_idx is None:
            raise RuntimeError(f"no completed track: {last}")
        mp3 = _http_bytes(f"/job/{job_id}/track/{track_idx}/mp3")
        OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
        out = OUTPUTS_DIR / f"khala_{job_id}_track{track_idx}.mp3"
        out.write_bytes(mp3)
        (OUTPUTS_DIR / f"khala_{job_id}_metadata.json").write_text(json.dumps({"request": payload, "job": last}, indent=2), encoding="utf-8")
        outputs.commit()
        print(f"[khala-modal] wrote {out} bytes={len(mp3)}")
        return out
    finally:
        _stop_backend(proc)


@app.function(
    gpu=os.environ.get("KHALA_MODAL_GPU", "L40S"),
    timeout=7200,
    volumes={str(CHECKPOINTS_DIR): checkpoints, str(OUTPUTS_DIR): outputs},
)
def generate(prompt: str, lyrics: str = "", duration: int = 1, mode: str = "vocal", prompt_mode: str = "natural", top_k_bb: int = 80, temperature: float = 1.0) -> bytes:
    """Generate a Khala MP3 and return its bytes."""
    out = _generate_once(prompt, lyrics, duration, mode, prompt_mode, top_k_bb, temperature)
    return out.read_bytes()


@app.function(
    gpu=os.environ.get("KHALA_MODAL_GPU", "L40S"),
    timeout=7200,
    volumes={str(CHECKPOINTS_DIR): checkpoints, str(OUTPUTS_DIR): outputs},
)
@modal.fastapi_endpoint(method="POST")
def generate_endpoint(item: dict) -> dict:
    """Optional HTTP endpoint; scale-to-zero GPU worker after each request."""
    prompt = item.get("prompt", "")
    lyrics = item.get("lyrics", "")
    duration = int(item.get("duration", 1))
    mode = item.get("mode", "vocal")
    prompt_mode = item.get("prompt_mode", "natural")
    top_k_bb = int(item.get("top_k_bb", 80))
    temperature = float(item.get("temperature", 1.0))
    out = _generate_once(prompt, lyrics, duration, mode, prompt_mode, top_k_bb, temperature)
    return {"ok": True, "modal_volume": "khala-outputs", "path": str(out), "bytes": out.stat().st_size}


@app.local_entrypoint()
def generate_cli(
    prompt: str,
    lyrics_file: str = "",
    lyrics: str = "",
    duration: int = 1,
    mode: str = "vocal",
    prompt_mode: str = "natural",
    top_k_bb: int = 80,
    temperature: float = 1.0,
    out: str = "/tmp/khala_sample.mp3",
):
    if lyrics_file:
        lyrics = pathlib.Path(lyrics_file).read_text(encoding="utf-8")
    audio = generate.remote(prompt, lyrics, duration, mode, prompt_mode, top_k_bb, temperature)
    pathlib.Path(out).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(out).write_bytes(audio)
    print(out)
