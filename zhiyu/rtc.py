import asyncio
import json
import logging
import time
from contextlib import suppress

import numpy as np
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamError
from av import AudioResampler

from .audio import ASR_RATE, RTC_RATE, WINDOW, CallRecording, Playback, Recognizer, clauses, pcm, resample
from .dialog import redact

LOG = logging.getLogger(__name__)


class VoiceCall:
    def __init__(self, dialog, speech, recording_dir, record=False, microphone=True):
        self.dialog = dialog
        self.speech = speech
        self.created = time.monotonic()
        self.recording = CallRecording(recording_dir, dialog.id) if record else None
        self.pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self.channel = None
        self.playback = Playback(self.first_audio, self.playback_finished, self.recording)
        self.pc.addTrack(self.playback)
        self.tasks = set()
        self.lock = asyncio.Lock()
        self.playback_done = asyncio.Event()
        self.playback_done.set()
        self.microphone = microphone
        self.mic_revision = 0
        self.reply_task = None
        self.result = None
        self.turn_end = None
        self.reply_session_turn = 0
        self.closed = False
        self.client_audio = set()
        self.audio_started = set()

        @self.pc.on("datachannel")
        def channel_available(channel):
            if channel.label != "control" or self.channel is not None:
                channel.close()
                return
            self.channel = channel

            @channel.on("message")
            def on_message(message):
                self.launch(self.control(message))

            @channel.on("open")
            def opened():
                self.send("ready", session_id=self.dialog.id)

            if channel.readyState == "open":
                opened()

        @self.pc.on("track")
        def track_received(track):
            if track.kind == "audio":
                self.launch(self.consume(track))

        @self.pc.on("connectionstatechange")
        async def connection_changed():
            state = self.pc.connectionState
            self.dialog.store.log(self.dialog.id, "rtc", state=state)
            self.send("rtc", state=state)
            if state in {"failed", "closed"} and not self.closed:
                self.launch(self.close())

    def launch(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)

        def done(finished):
            self.tasks.discard(finished)
            if not finished.cancelled() and finished.exception():
                LOG.error("WebRTC session %s", self.dialog.id, exc_info=finished.exception())
                self.send("error", detail="语音链路发生异常，请重新发起通话。")

        task.add_done_callback(done)
        return task

    async def negotiate(self, sdp):
        await self.pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="offer"))
        if not any(item.kind == "audio" for item in self.pc.getTransceivers()):
            raise ValueError("SDP 必须协商音频")
        await self.pc.setLocalDescription(await self.pc.createAnswer())
        return self.pc.localDescription.sdp

    def send(self, kind, **data):
        if (not self.closed and self.pc.connectionState == "connected"
                and self.channel is not None and self.channel.readyState == "open"):
            self.channel.send(json.dumps({"type": kind, **data}, ensure_ascii=False))

    async def control(self, message):
        if not isinstance(message, str) or len(message) > 4096 or self.closed:
            return
        try:
            data = json.loads(message)
        except ValueError:
            return
        if not isinstance(data, dict):
            return
        kind = data.get("type")
        if kind == "start" and self.dialog.state == "idle":
            async with self.lock:
                if self.dialog.state == "idle":
                    response = await asyncio.to_thread(self.dialog.start)
                    self.say(response)
        elif kind == "microphone" and type(data.get("enabled")) is bool:
            enabled = data["enabled"]
            if self.microphone != enabled:
                self.microphone = enabled
                self.mic_revision += 1
                self.dialog.store.log(self.dialog.id, "microphone", enabled=enabled)
        elif kind in {"no_answer", "hangup", "retry"}:
            async with self.lock:
                if self.dialog.state not in {"done", "idle"}:
                    self.cancel_audio()
                    response = await asyncio.to_thread(self.dialog.terminal, kind)
                    self.say(response)
        elif kind == "client_audio":
            turn, ms = data.get("turn"), data.get("ms")
            if (isinstance(turn, int) and turn in self.audio_started and turn not in self.client_audio
                    and isinstance(ms, (int, float)) and 0 < ms < 15000):
                self.client_audio.add(turn)
                self.dialog.store.log(self.dialog.id, "client_audio", turn=turn, ms=round(ms, 1))
        elif kind == "rtc_stats":
            fields = ("rtt_ms", "jitter_ms", "loss", "packets", "concealed_samples")
            values = {key: data[key] for key in fields
                      if isinstance(data.get(key), (float, int)) and 0 <= data[key] <= 100_000_000}
            if values:
                self.dialog.store.log(self.dialog.id, "rtc_stats", **values)

    async def consume(self, track):
        recognizer = None
        revision = -1
        convert = AudioResampler(format="s16", layout="mono", rate=ASR_RATE)
        pending = np.zeros(0, dtype=np.float32)
        try:
            while not self.closed:
                frame = await track.recv()
                if not self.microphone:
                    continue
                if revision != self.mic_revision:
                    revision = self.mic_revision
                    pending = np.zeros(0, dtype=np.float32)
                    recognizer = await asyncio.to_thread(Recognizer, self.speech)
                    convert = AudioResampler(format="s16", layout="mono", rate=ASR_RATE)
                for converted in convert.resample(frame):
                    samples = converted.to_ndarray().reshape(-1).astype(np.float32) / 32768
                    if self.recording:
                        self.recording.add(0, pcm(resample(samples, ASR_RATE, RTC_RATE)))
                    pending = np.concatenate((pending, samples))
                    while len(pending) >= WINDOW:
                        block, pending = pending[:WINDOW], pending[WINDOW:]
                        if self.dialog.state in {"idle", "done"} or revision != self.mic_revision:
                            continue
                        notices = await asyncio.to_thread(recognizer.push, block, time.monotonic())
                        if revision == self.mic_revision:
                            self.consume_notices(notices, revision)
        except MediaStreamError:
            pass

    def consume_notices(self, notices, revision):
        for kind, value in notices:
            if kind == "speech_start":
                self.send("speech_start")
                if self.playback.active:
                    self.interrupt(value)
            elif kind == "partial":
                text = self.dialog.knowledge.transcript(redact(value))
                self.send("partial", text=text)
            elif kind == "final":
                text, end, first = value
                self.send("speech_end")
                if text:
                    self.launch(self.process(text, end, first, revision))
                else:
                    self.dialog.store.log(self.dialog.id, "failure", reason="asr_empty")
                    self.send("partial", text="")

    async def process(self, text, end=None, first=None, revision=None):
        async with self.lock:
            if revision is None:
                await self.playback_done.wait()
            elif revision != self.mic_revision:
                return False
            if self.closed or self.dialog.state in {"idle", "done"}:
                return False
            mode = "text" if revision is None else "voice"
            start = time.monotonic()
            response = await asyncio.to_thread(self.dialog.handle, text, input_mode=mode)
            elapsed = time.monotonic() - start
            self.dialog.store.log(
                self.dialog.id, "timing", turn=response["turn"], input_mode=mode,
                endpoint_ms=round((start - end) * 1000, 1) if end is not None else None,
                policy_ms=round(elapsed * 1000, 1), first_partial=first is not None)
            self.send("utterance", text=self.dialog.knowledge.transcript(redact(text))[:400], turn=response["turn"],
                      input_mode=mode)
            self.say(response, end)
            return True

    def interrupt(self, speech_start):
        intent = self.result["intent"] if self.result else ""
        self.dialog.interrupt(intent)
        self.cancel_audio()
        ms = round((time.monotonic() - speech_start) * 1000, 1)
        self.dialog.store.log(self.dialog.id, "interrupt", turn=self.reply_session_turn, stop_ms=ms)
        self.send("interrupted", turn=self.reply_session_turn, stop_ms=ms)

    def cancel_audio(self):
        self.playback.clear()
        self.playback_done.set()
        if self.reply_task and not self.reply_task.done():
            self.reply_task.cancel()

    def say(self, response, end=None):
        self.cancel_audio()
        self.result = response
        self.turn_end = end
        self.reply_session_turn = response["turn"]
        generation = self.playback.begin()
        self.playback_done.clear()
        self.send("reply", **response)
        self.reply_task = self.launch(self.synthesize(generation, response["text"]))

    async def synthesize(self, generation, text):
        start = time.monotonic()
        first = True
        try:
            for part in clauses(text):
                samples, rate = await asyncio.to_thread(self.speech.synthesize, part)
                if not samples.size:
                    continue
                self.playback.add(generation, pcm(resample(samples, rate, RTC_RATE)))
                if first:
                    self.dialog.store.log(self.dialog.id, "tts", turn=self.reply_session_turn,
                                          first_chunk_ms=round((time.monotonic() - start) * 1000, 1))
                    first = False
            if first:
                self.dialog.store.log(self.dialog.id, "failure", reason="tts_empty")
        finally:
            self.playback.finish(generation)

    def first_audio(self, now):
        ms = round((now - self.turn_end) * 1000, 1) if self.turn_end is not None else None
        if ms is not None:
            self.audio_started.add(self.reply_session_turn)
        self.dialog.store.log(self.dialog.id, "first_audio", turn=self.reply_session_turn, ms=ms)
        self.send("audio_start", turn=self.reply_session_turn, server_ms=ms)

    def playback_finished(self):
        self.playback_done.set()
        self.send("audio_end", turn=self.reply_session_turn)

    async def close(self):
        if self.closed:
            return
        self.closed = True
        self.cancel_audio()
        self.playback.stop()
        await self.pc.close()
        async with self.lock:
            if self.dialog.state not in {"done", "idle"}:
                with suppress(ValueError, OSError):
                    await asyncio.to_thread(self.dialog.terminal, "disconnect")
        pending = [task for task in self.tasks if task is not asyncio.current_task()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if self.recording:
            await asyncio.to_thread(self.recording.save)
