import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np

from zhiyu.audio import FRAME, Playback, clauses
from zhiyu.rtc import VoiceCall


def test_tts_keeps_complete_phrases_and_lists():
    first = "您好，核对授权书、商标注册证及申请材料中的有效日期后，请在原驳回单据上传并重新提交。"
    assert list(clauses(first + "请问整改步骤清楚了吗？")) == [first, "请问整改步骤清楚了吗？"]


def test_playback_buffers_short_chunks_and_excludes_preroll_from_first_audio():
    async def check():
        started, finished = Mock(), Mock()
        playback = Playback(started, finished)
        playback.BUFFER_SAMPLES = 2 * FRAME
        generation = playback.begin()
        silent = np.zeros(FRAME, dtype=np.int16)
        audible = np.full(FRAME, 1100, dtype=np.int16)

        playback.add(generation, silent)
        assert not (await playback.recv()).to_ndarray().any()
        assert playback.buffered_samples == FRAME
        playback.add(generation, audible)
        assert not (await playback.recv()).to_ndarray().any()
        started.assert_not_called()
        assert np.array_equal((await playback.recv()).to_ndarray().reshape(-1), audible)
        started.assert_called_once()

        assert not (await playback.recv()).to_ndarray().any()
        assert playback.underrun_samples == FRAME
        playback.add(generation, audible)
        playback.finish(generation)
        assert np.array_equal((await playback.recv()).to_ndarray().reshape(-1), audible)
        assert not (await playback.recv()).to_ndarray().any()
        finished.assert_called_once()

    asyncio.run(check())


def test_barge_in_keeps_output_for_empty_or_stale_audio_but_stops_on_recognized_speech():
    call = VoiceCall.__new__(VoiceCall)
    call.playback = SimpleNamespace(active=True, generation=3)
    call.possible_barge_in = None
    call.dialog = SimpleNamespace(id="voice", knowledge=SimpleNamespace(transcript=lambda text: text), store=Mock())
    call.send, call.interrupt = Mock(), Mock()

    call.consume_notices([("speech_start", 10), ("final", ("", 10.4, None))], 0)
    call.interrupt.assert_not_called()
    call.dialog.store.log.assert_called_once_with("voice", "barge_guard", reason="asr_empty")

    call.consume_notices([("speech_start", 10.5), ("partial", "嗯嗯"), ("final", ("嗯嗯", 10.8, 10.7))], 0)
    call.interrupt.assert_not_called()
    call.dialog.store.log.assert_called_with("voice", "barge_guard", reason="backchannel")

    call.consume_notices([("speech_start", 11)], 0)
    call.playback.generation = 4
    call.consume_notices([("partial", "请稍等")], 0)
    call.interrupt.assert_not_called()

    call.consume_notices([("speech_start", 12), ("partial", "请稍等")], 0)
    call.interrupt.assert_called_once_with(12)
