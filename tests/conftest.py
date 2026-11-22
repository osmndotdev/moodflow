import json
import warnings
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from backend.schemas import Track

ROOT = Path(__file__).resolve().parent.parent
warnings.filterwarnings("ignore", module="numba")


@pytest.fixture
def sample_tracks() -> list[Track]:
    data = json.loads((ROOT / "fixtures" / "sample_playlist.json").read_text())
    return [Track(**t) for t in data["tracks"]]


def make_beat_wav(path: Path, bpm: float = 120, seconds: float = 60, intro_silence: float = 0,
                  amplitude: float = 0.5, freq: float = 220, sr: int = 44100) -> Path:
    """Synthetic 'song': a tone bed plus kick-like clicks on every beat."""
    t = np.arange(int(seconds * sr)) / sr
    bed = 0.3 * np.sin(2 * np.pi * freq * t)
    kick = np.zeros_like(t)
    period = 60.0 / bpm
    env_len = int(0.08 * sr)
    env = np.exp(-np.linspace(0, 8, env_len)) * np.sin(2 * np.pi * 60 * np.arange(env_len) / sr)
    for b in np.arange(0, seconds, period):
        i = int(b * sr)
        kick[i:i + env_len] += env[: max(0, min(env_len, len(t) - i))]
    y = amplitude * (bed + kick) / 1.3
    if intro_silence:
        y[: int(intro_silence * sr)] = 0
    sf.write(path, np.stack([y, y], axis=1), sr)
    return path


@pytest.fixture
def beat_wav(tmp_path):
    def _make(name: str, **kw) -> Path:
        return make_beat_wav(tmp_path / f"{name}.wav", **kw)
    return _make
