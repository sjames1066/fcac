"""Join announcement pieces (.ogg) into one .ogg file.

Uses `soundfile` + `numpy` (bundled in FCAC.exe). If soundfile is not installed it falls back to ffmpeg when that
is on the PATH, so running FCAC from source still works.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import numpy as np

try:
    import soundfile as sf
except Exception:  # not installed / libsndfile missing
    sf = None

SILENCE = 0.004  # amplitude below this counts as silence at a clip's edges (about -48 dB)


class AudioError(Exception):
    pass


def have_backend() -> bool:
    return sf is not None or shutil.which("ffmpeg") is not None


# --------------------------------------------------------------------------- reading / writing
def _read(path: Path) -> tuple[np.ndarray, int]:
    """-> (samples as float32 [frames, channels], sample rate)"""
    if sf is not None:
        try:
            data, rate = sf.read(str(path), dtype="float32", always_2d=True)
            return data, int(rate)
        except Exception as exc:
            raise AudioError(f"Could not read {path.name}: {exc}") from exc
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise AudioError("No audio library: install soundfile (pip install soundfile) or ffmpeg.")
    probe = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=sample_rate,channels",
         "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    try:
        rate, channels = (int(x) for x in probe.stdout.strip().split(",")[:2])
    except ValueError as exc:
        raise AudioError(f"Could not read {path.name}") from exc
    raw = subprocess.run([ffmpeg, "-v", "error", "-i", str(path), "-f", "f32le", "-"], capture_output=True)
    if raw.returncode:
        raise AudioError(f"Could not read {path.name}")
    return np.frombuffer(raw.stdout, dtype="<f4").reshape(-1, channels).copy(), rate


def _write(path: Path, data: np.ndarray, rate: int) -> None:
    data = np.clip(data, -1.0, 1.0).astype("float32")
    if sf is not None:
        try:
            sf.write(str(path), data, rate, format="OGG", subtype="VORBIS")
            return
        except Exception as exc:
            raise AudioError(f"Could not write {path.name}: {exc}") from exc
    ffmpeg = shutil.which("ffmpeg")
    done = subprocess.run(
        [ffmpeg, "-v", "error", "-y", "-f", "f32le", "-ar", str(rate), "-ac", str(data.shape[1]), "-i", "-",
         "-c:a", "libvorbis", "-q:a", "5", "-f", "ogg", str(path)],
        input=data.astype("<f4").tobytes(), capture_output=True)
    if done.returncode:
        raise AudioError(f"Could not write {path.name}: {done.stderr.decode(errors='replace')[:200]}")


# --------------------------------------------------------------------------- editing
def _resample(data: np.ndarray, src: int, dst: int) -> np.ndarray:
    if src == dst or len(data) == 0:
        return data
    n = max(1, int(round(len(data) * dst / src)))
    x_old = np.linspace(0.0, 1.0, len(data), endpoint=False)
    x_new = np.linspace(0.0, 1.0, n, endpoint=False)
    return np.stack([np.interp(x_new, x_old, data[:, c]) for c in range(data.shape[1])], axis=1).astype("float32")


def _channels(data: np.ndarray, count: int) -> np.ndarray:
    if data.shape[1] == count:
        return data
    if data.shape[1] == 1:
        return np.repeat(data, count, axis=1)
    return data[:, :count] if data.shape[1] > count else np.hstack([data, np.repeat(data[:, -1:], count - data.shape[1], axis=1)])


def _trim(data: np.ndarray, rate: int, keep_ms: int = 12) -> np.ndarray:
    """Cut silence off both ends, leaving a few ms so the sound isn't clipped."""
    loud = np.flatnonzero(np.abs(data).max(axis=1) > SILENCE)
    if len(loud) == 0:
        return data
    keep = int(rate * keep_ms / 1000)
    return data[max(0, loud[0] - keep): loud[-1] + 1 + keep]


def _fade(data: np.ndarray, rate: int, ms: int = 6) -> np.ndarray:
    n = min(int(rate * ms / 1000), len(data) // 2)
    if n > 0:
        ramp = np.linspace(0.0, 1.0, n, dtype="float32")[:, None]
        data = data.copy()
        data[:n] *= ramp
        data[-n:] *= ramp[::-1]
    return data


def join(paths: list[Path], out: Path, pause_ms: int = 60) -> None:
    """Play `paths` one after another and save as `out` (.ogg). Edges are trimmed and faded so the joins are clean;
    `pause_ms` is the gap left between pieces."""
    if not paths:
        raise AudioError("Nothing to join.")
    clips = [_read(Path(p)) for p in paths]
    rate = clips[0][1]
    channels = max(d.shape[1] for d, _ in clips)
    pieces = []
    for data, src_rate in clips:
        data = _channels(_resample(data, src_rate, rate), channels)
        pieces.append(_fade(_trim(data, rate), rate))
    gap = np.zeros((int(rate * max(0, pause_ms) / 1000), channels), dtype="float32")
    parts = []
    for i, piece in enumerate(pieces):
        if i and len(gap):
            parts.append(gap)
        parts.append(piece)
    out.parent.mkdir(parents=True, exist_ok=True)
    _write(out, np.concatenate(parts), rate)
