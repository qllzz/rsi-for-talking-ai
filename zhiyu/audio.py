import asyncio
import math
import os
import re
import time
import wave
from collections import deque
from fractions import Fraction
from functools import lru_cache
from pathlib import Path
from threading import Lock

import numpy as np
import sherpa_onnx
from aiortc import AudioStreamTrack
from av import AudioFrame

from .knowledge import ROOT

ASR_RATE = 16000
RTC_RATE = 48000
WINDOW = 512
FRAME = 960
MODELS = ROOT / "models"
ASR_FILES = ("asr/model.int8.onnx", "asr/tokens.txt", "asr/bbpe.model", "silero_vad.onnx")
MATCHA_DIR = "matcha-icefall-zh-en"
VOCOS = "vocos-16khz-univ.onnx"
MATCHA_FILES = ("model-steps-3.onnx", "tokens.txt", "lexicon.txt", "date-zh.fst", "number-zh.fst",
                "phone-zh.fst", "espeak-ng-data/phondata")
MELO_FILES = ("model.int8.onnx", "lexicon.txt", "tokens.txt", "number.fst", "phone.fst", "date.fst")


def speech_engine(engine=None):
    engine = engine or os.environ.get("ZHIYU_TTS_ENGINE", "matcha")
    if engine not in {"matcha", "melo"}:
        raise ValueError("ZHIYU_TTS_ENGINE 必须为 matcha 或 melo")
    return engine


def missing_models(engine=None):
    tts_files = ((VOCOS, *(f"{MATCHA_DIR}/{file}" for file in MATCHA_FILES)) if speech_engine(engine) == "matcha"
                 else tuple(f"tts/{file}" for file in MELO_FILES))
    required = (*ASR_FILES, *tts_files)
    return [name for name in required if not (MODELS / name).is_file()]


def native_path(path):
    # The Windows ONNX launcher opens narrow paths. The launchers set cwd to the project root.
    return os.path.relpath(path) if os.name == "nt" else str(path)


def resample(samples, source_rate, target_rate):
    samples = np.asarray(samples, dtype=np.float32)
    if source_rate == target_rate or not samples.size:
        return samples
    positions = np.arange(round(len(samples) * target_rate / source_rate)) * (source_rate / target_rate)
    return np.interp(positions, np.arange(len(samples)), samples).astype(np.float32)


def pcm(samples):
    return (np.clip(samples, -1, 1) * 32767).astype(np.int16)


class SpeechModels:
    def __init__(self, *, engine=None):
        self.tts_engine = speech_engine(engine)
        missing = missing_models(self.tts_engine)
        if missing:
            raise FileNotFoundError("缺少离线模型 " + ", ".join(missing))
        asr = MODELS / "asr"
        self.asr = sherpa_onnx.OnlineRecognizer.from_zipformer2_ctc(
            tokens=native_path(asr / "tokens.txt"), model=native_path(asr / "model.int8.onnx"), num_threads=2,
            sample_rate=ASR_RATE, feature_dim=80, decoding_method="greedy_search")
        self.asr_lock = Lock()
        self.tts_speed = float(os.environ.get("ZHIYU_TTS_SPEED", "1.0" if self.tts_engine == "matcha" else "1.1"))
        if not math.isfinite(self.tts_speed) or not 0.8 <= self.tts_speed <= 1.25:
            raise ValueError("ZHIYU_TTS_SPEED 必须在 0.8 到 1.25 之间")
        self.tts = self._load_tts()
        self.tts_lock = Lock()

    def _load_tts(self):
        if self.tts_engine == "matcha":
            folder = MODELS / MATCHA_DIR
            custom = ROOT / "data" / "matcha_zh.lexicon"
            lexicon = ",".join(map(native_path, (custom, folder / "lexicon.txt")))
            matcha = sherpa_onnx.OfflineTtsMatchaModelConfig(
                acoustic_model=native_path(folder / "model-steps-3.onnx"), vocoder=native_path(MODELS / VOCOS),
                lexicon=lexicon, tokens=native_path(folder / "tokens.txt"), data_dir=native_path(folder / "espeak-ng-data"))
            model = sherpa_onnx.OfflineTtsModelConfig(matcha=matcha, num_threads=3, provider="cpu")
            return self._create_tts(model, folder, ("phone-zh.fst", "date-zh.fst", "number-zh.fst"))

        folder = MODELS / "tts"
        vits = sherpa_onnx.OfflineTtsVitsModelConfig(
            model=native_path(folder / "model.int8.onnx"), lexicon=native_path(folder / "lexicon.txt"),
            tokens=native_path(folder / "tokens.txt"), noise_scale=0.45, noise_scale_w=0.55)
        model = sherpa_onnx.OfflineTtsModelConfig(vits=vits, num_threads=3, provider="cpu")
        return self._create_tts(model, folder, ("phone.fst", "date.fst", "number.fst"))

    @staticmethod
    def _create_tts(model, folder, rules):
        config = sherpa_onnx.OfflineTtsConfig(
            model=model, rule_fsts=",".join(native_path(folder / name) for name in rules),
            max_num_sentences=1, silence_scale=0.25)
        if not config.validate():
            raise ValueError("TTS 模型配置不完整")
        return sherpa_onnx.OfflineTts(config)

    @lru_cache(maxsize=256)
    def synthesize(self, text, speed=None):
        with self.tts_lock:
            result = self.tts.generate(text, sid=0, speed=self.tts_speed if speed is None else speed)
        return np.asarray(result.samples, dtype=np.float32), result.sample_rate

    def parts(self, text):
        # Matcha can synthesize a full reply before it is played. The older VITS
        # fallback still delivers complete sentences as they become available.
        return (text,) if self.tts_engine == "matcha" else clauses(text)

    def warm(self):
        self.synthesize("您好。")
        stream = self.asr.create_stream()
        stream.accept_waveform(ASR_RATE, np.zeros(ASR_RATE, dtype=np.float32))
        self.decode(stream)

    def prewarm(self, scripts):
        prefixes = dict.fromkeys(next(iter(self.parts(script))) for script in scripts if script)
        for phrase in prefixes:
            self.synthesize(phrase)

    def decode(self, stream):
        with self.asr_lock:
            while self.asr.is_ready(stream):
                self.asr.decode_stream(stream)
            return self.asr.get_result(stream)

    def make_vad(self):
        config = sherpa_onnx.VadModelConfig()
        config.silero_vad.model = native_path(MODELS / "silero_vad.onnx")
        config.silero_vad.threshold = 0.50
        config.silero_vad.min_speech_duration = 0.064
        # A short pause at a comma is usually still part of the same customer turn.
        config.silero_vad.min_silence_duration = 0.42
        config.silero_vad.window_size = WINDOW
        config.sample_rate = ASR_RATE
        config.num_threads = 1
        return sherpa_onnx.VoiceActivityDetector(config, buffer_size_in_seconds=15)


class Recognizer:
    """Consumes 32 ms windows, feeds a streaming decoder and emits partial/final turns."""

    def __init__(self, models):
        self.models = models
        self.vad = models.make_vad()
        self.stream = None
        self.preroll = deque(maxlen=9)
        self.windows = 0
        self.partial = ""
        self.speech_end = 0.0
        self.first_partial = None
        self.noise = 0.001

    def push(self, block, now):
        notices = []
        self.vad.accept_waveform(block)
        speaking = self.vad.is_speech_detected()
        energy = float(np.sqrt(np.mean(block * block)))
        if not speaking and self.stream is None:
            self.noise = min(0.025, 0.95 * self.noise + 0.05 * energy)
        if speaking and self.stream is None:
            self.stream = self.models.asr.create_stream()
            self.windows = 0
            self.partial = ""
            self.first_partial = None
            self.speech_end = now
            for prior in self.preroll:
                self.stream.accept_waveform(ASR_RATE, prior)
            notices.append(("speech_start", now - 0.064))
        self.preroll.append(block)
        if self.stream is not None:
            self.windows += 1
            self.stream.accept_waveform(ASR_RATE, block)
            if speaking and energy > max(0.004, self.noise * 2.0):
                self.speech_end = now
            result = self.models.decode(self.stream)
            if result and result != self.partial:
                self.partial = result
                self.first_partial = self.first_partial or now
                notices.append(("partial", result))
            if not speaking or self.windows >= 350:
                self.stream.accept_waveform(ASR_RATE, np.zeros(5120, dtype=np.float32))
                self.stream.input_finished()
                final = self.models.decode(self.stream).strip()
                notices.append(("final", (final, self.speech_end, self.first_partial)))
                self.stream = None
        while not self.vad.empty():
            self.vad.pop()
        return notices


def clauses(text):
    # Commas and enumeration marks carry phrasing and pronunciation context.
    for sentence in re.findall(r"[^。！？；!?;\r\n]+[。！？；!?;]*", text):
        if sentence.strip():
            yield sentence.strip()


class CallRecording:
    def __init__(self, folder: Path, call_id: str):
        self.path = folder / f"{call_id}.wav"
        self.started = time.monotonic()
        self.tracks = [[], []]

    def add(self, channel, samples):
        offset = round((time.monotonic() - self.started) * RTC_RATE)
        if offset < 300 * RTC_RATE:
            self.tracks[channel].append((offset, samples.copy()))

    def save(self):
        length = max((offset + len(samples) for track in self.tracks for offset, samples in track),
                     default=0)
        if not length:
            return
        data = np.zeros((length, 2), dtype=np.int16)
        for channel, segments in enumerate(self.tracks):
            for offset, samples in segments:
                data[offset:offset + len(samples), channel] = samples
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(self.path), "wb") as file:
            file.setnchannels(2)
            file.setsampwidth(2)
            file.setframerate(RTC_RATE)
            file.writeframes(data.tobytes())


class Playback(AudioStreamTrack):
    BUFFER_SAMPLES = RTC_RATE * 3 // 10
    AUDIBLE_PEAK = 164

    def __init__(self, first_frame, finished, recording=None):
        super().__init__()
        self.on_first_frame = first_frame
        self.on_finished = finished
        self.recording = recording
        self.chunks = deque()
        self.started = None
        self.timestamp = 0
        self.generation = 0
        self.first = False
        self.producing = False
        self.active = False
        self.buffered_samples = 0
        self.underrun_samples = 0
        self.waiting_for_buffer = True

    def begin(self):
        self.clear()
        self.producing = True
        self.active = True
        return self.generation

    def add(self, generation, samples):
        if generation == self.generation and len(samples):
            self.chunks.append(samples)
            self.buffered_samples += len(samples)

    def finish(self, generation):
        if generation == self.generation:
            self.producing = False

    def clear(self):
        self.generation += 1
        self.chunks.clear()
        self.first = False
        self.producing = False
        self.active = False
        self.buffered_samples = 0
        self.underrun_samples = 0
        self.waiting_for_buffer = True

    async def recv(self):
        if self.started is None:
            self.started = time.monotonic()
        else:
            self.timestamp += FRAME
            target = self.started + self.timestamp / RTC_RATE
            overdue = time.monotonic() - target
            if overdue > 0.12:
                self.started += overdue
            elif overdue < 0:
                await asyncio.sleep(-overdue)
        data = np.zeros(FRAME, dtype=np.int16)
        used = 0
        if self.waiting_for_buffer and (self.buffered_samples >= self.BUFFER_SAMPLES or not self.producing):
            self.waiting_for_buffer = False
        while self.chunks and used < FRAME and not self.waiting_for_buffer:
            block = self.chunks[0]
            count = min(FRAME - used, len(block))
            data[used:used + count] = block[:count]
            used += count
            if count == len(block):
                self.chunks.popleft()
            else:
                self.chunks[0] = block[count:]
        self.buffered_samples -= used
        if self.first and self.producing and used < FRAME:
            self.underrun_samples += FRAME - used
            self.waiting_for_buffer = True
        if used and not self.first and ((data > self.AUDIBLE_PEAK).any() or (data < -self.AUDIBLE_PEAK).any()):
            self.first = True
            self.on_first_frame(time.monotonic())
        if not used and self.active and not self.producing:
            self.active = False
            self.on_finished()
        if used and self.recording:
            self.recording.add(1, data)
        frame = AudioFrame.from_ndarray(data.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = RTC_RATE
        frame.pts = self.timestamp
        frame.time_base = Fraction(1, RTC_RATE)
        return frame
