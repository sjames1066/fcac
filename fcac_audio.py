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


# --------------------------------------------------------------------------- loudness and the cabin-PA sound
def _biquad_response(b: tuple, a: tuple, freqs: np.ndarray, rate: int) -> np.ndarray:
    w = 2 * np.pi * freqs / rate
    z1, z2 = np.exp(-1j * w), np.exp(-2j * w)
    return (b[0] + b[1] * z1 + b[2] * z2) / (1 + a[0] * z1 + a[1] * z2)


def _k_weight_response(freqs: np.ndarray, rate: int) -> np.ndarray:
    """ITU-R BS.1770 K-weighting (high shelf + 38 Hz high-pass) as a frequency response."""
    k = np.tan(np.pi * 1681.974450955533 / rate)
    q, vh = 0.7071752369554196, 10 ** (3.999843853973347 / 20)
    vb = vh ** 0.4996667741545416
    a0 = 1 + k / q + k * k
    shelf_b = ((vh + vb * k / q + k * k) / a0, 2 * (k * k - vh) / a0, (vh - vb * k / q + k * k) / a0)
    shelf_a = (2 * (k * k - 1) / a0, (1 - k / q + k * k) / a0)
    k = np.tan(np.pi * 38.13547087602444 / rate)
    q = 0.5003270373238773
    a0 = 1 + k / q + k * k
    return _biquad_response(shelf_b, shelf_a, freqs, rate) * _biquad_response(
        (1.0, -2.0, 1.0), (2 * (k * k - 1) / a0, (1 - k / q + k * k) / a0), freqs, rate)


def _apply_response(x: np.ndarray, rate: int, response) -> np.ndarray:
    """Filter a mono signal by multiplying its spectrum by response(freqs) (zero phase)."""
    n = len(x)
    size = 1 << int(np.ceil(np.log2(n + rate // 4)))
    spec = np.fft.rfft(x, size)
    spec *= response(np.fft.rfftfreq(size, 1.0 / rate))
    return np.fft.irfft(spec, size)[:n].astype("float32")


def loudness_lufs(data: np.ndarray, rate: int) -> float | None:
    """Integrated loudness (ITU-R BS.1770-4 with gating) of float audio [frames, channels]. None for silence."""
    if len(data) == 0:
        return None
    weighted = [_apply_response(data[:, c], rate, lambda f: np.abs(_k_weight_response(f, rate)))
                for c in range(data.shape[1])]
    block = min(int(0.4 * rate), len(data))
    hop = max(1, int(0.1 * rate))
    starts = range(0, max(1, len(data) - block + 1), hop)
    energy = np.array([sum(float(np.mean(w[s:s + block] ** 2)) for w in weighted) for s in starts])
    loud = -0.691 + 10 * np.log10(np.maximum(energy, 1e-12))
    kept = energy[loud > -70.0]
    if len(kept) == 0:
        return None
    relative = -0.691 + 10 * np.log10(kept.mean()) - 10.0
    kept = energy[loud > max(-70.0, relative)]
    if len(kept) == 0:
        return None
    return float(-0.691 + 10 * np.log10(kept.mean()))


def file_loudness(path: Path) -> float | None:
    data, rate = _read(Path(path))
    return loudness_lufs(data, rate)


def _compress(x: np.ndarray, rate: int, threshold_db: float = -26.0, ratio: float = 3.0) -> np.ndarray:
    """Gentle feed-forward compressor: evens out loud and quiet words."""
    step = max(1, int(rate * 0.0025))
    frames = len(x) // step + 1
    padded = np.zeros(frames * step, dtype="float32")
    padded[:len(x)] = x
    rms = np.sqrt(np.mean(padded.reshape(frames, step) ** 2, axis=1) + 1e-12)
    attack, release = np.exp(-step / (rate * 0.005)), np.exp(-step / (rate * 0.12))
    env, level = np.empty(frames, dtype="float32"), 0.0
    for i, v in enumerate(rms):  # peak-following envelope, fast up and slow down
        level = attack * level + (1 - attack) * v if v > level else release * level + (1 - release) * v
        env[i] = level
    over = np.maximum(20 * np.log10(env + 1e-9) - threshold_db, 0.0)
    gain = 10 ** (-over * (1 - 1 / ratio) / 20)
    gain = np.interp(np.arange(len(x)), np.arange(frames) * step + step / 2, gain)
    return (x * gain).astype("float32")


def pa_effect(data: np.ndarray, rate: int) -> np.ndarray:
    """The FCAC cabin-PA sound: band-limited speaker voice, a little presence, gentle compression and warmth."""
    mono = data.mean(axis=1) if data.shape[1] > 1 else data[:, 0]

    def shape(f):
        f = np.maximum(f, 1.0)
        high_pass = f ** 2 / np.sqrt(f ** 4 + 300.0 ** 4)            # 300 Hz, 2nd order
        low_pass = 1.0 / np.sqrt(1.0 + (f / 4200.0) ** 8)             # 4.2 kHz, 4th order
        presence = 1.0 + 0.6 * np.exp(-0.5 * (np.log2(f / 1800.0) / 0.55) ** 2)  # about +4 dB near 1.8 kHz
        return high_pass * low_pass * presence

    y = _apply_response(mono.astype("float32"), rate, shape)
    peak = float(np.max(np.abs(y))) or 1.0
    y = _compress(y / peak * 0.5, rate)
    drive = 1.8
    y = np.tanh(drive * y) / np.tanh(drive)  # soft saturation
    return y[:, None].astype("float32")


def _limit(x: np.ndarray, ceiling: float = 0.89) -> np.ndarray:
    knee = 0.7
    mag = np.abs(x)
    over = mag > knee
    out = x.copy()
    out[over] = np.sign(x[over]) * (knee + (ceiling - knee) * np.tanh((mag[over] - knee) / (ceiling - knee)))
    return out


def process_clip(src: Path, dst: Path, target_lufs: float, effect: bool = True) -> dict:
    """Read any audio file, optionally apply the PA sound, set its loudness to target_lufs and save an .ogg.
    Returns {"seconds", "lufs_in", "lufs_out"}."""
    data, rate = _read(Path(src))
    data = _trim(data, rate, 40)
    lufs_in = loudness_lufs(data, rate)
    if lufs_in is None or lufs_in < -60:
        raise AudioError("No sound was picked up in that recording. If you recorded it, check your microphone in Audio settings.")
    if effect:
        data = pa_effect(data, rate)
    now = loudness_lufs(data, rate)
    if now is not None:
        data = data * float(10 ** ((target_lufs - now) / 20))
    data = _fade(_limit(data), rate, 8)
    lufs_out = loudness_lufs(data, rate)
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    _write(Path(dst), data, rate)
    return {"seconds": round(len(data) / rate, 2),
            "lufs_in": None if lufs_in is None else round(lufs_in, 1),
            "lufs_out": None if lufs_out is None else round(lufs_out, 1)}


def convert_music(src: Path, dst: Path) -> dict:
    """Boarding music: no PA sound, no loudness change. An .ogg is copied as it is; anything else is converted."""
    src, dst = Path(src), Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    data, rate = _read(src)
    if src.suffix.lower() == ".ogg":
        shutil.copyfile(src, dst)
    else:
        _write(dst, data, rate)
    lufs = loudness_lufs(data, rate)
    return {"seconds": round(len(data) / rate, 1), "lufs_in": None if lufs is None else round(lufs, 1),
            "lufs_out": None if lufs is None else round(lufs, 1), "music": True}
