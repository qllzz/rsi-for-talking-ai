"""Run the normal browser workflow using generated speech as the test microphone."""
import argparse
import base64
import io
import json
import os
import subprocess
import time
import wave
from pathlib import Path

import httpx
import imageio_ffmpeg
import numpy as np
from playwright.sync_api import expect, sync_playwright

from zhiyu.audio import SpeechModels, pcm
from zhiyu.knowledge import compact

LINES = [
    "是的，我是负责人，请说。",
    "可以，请给我说明驳回的原因。",
    "这个材料应该在哪上传？",
    "好的，我明白了。",
    "好的，我听清楚了。",
]

TEST_MICROPHONE = """
(() => {
  let context, destination;
  let played = 0;
  window.micRequests = 0;
  navigator.mediaDevices.getUserMedia = async () => {
    window.micRequests++;
    context = new AudioContext();
    await context.resume();
    destination = context.createMediaStreamDestination();
    const silence = context.createConstantSource();
    silence.offset.value = 0;
    silence.connect(destination);
    silence.start();
    return destination.stream;
  };
  window.feedVoice = async (base64) => {
    const samples = Uint8Array.from(atob(base64), c => c.charCodeAt(0));
    const sound = context.createBufferSource();
    sound.buffer = await context.decodeAudioData(samples.buffer);
    sound.connect(destination);
    const finished = new Promise(resolve => {
      sound.onended = () => { played++; resolve(played); };
    });
    sound.start();
    return finished;
  };
})();
"""


def wave_bytes(samples, rate):
    output = io.BytesIO()
    with wave.open(output, "wb") as file:
        file.setframerate(rate)
        file.setnchannels(1)
        file.setsampwidth(2)
        file.writeframes(pcm(samples).tobytes())
    return output.getvalue()


def fixtures(models, noise_db, lines):
    results = []
    rng = np.random.default_rng(19)
    for text in lines:
        samples, rate = models.synthesize(text, speed=1.08)
        if noise_db is not None:
            noise = rng.normal(0, 1, len(samples))
            noise = np.convolve(noise, np.array([.17, .33, .33, .17]), mode="same")
            rms = np.sqrt(np.mean(samples * samples))
            samples = samples + noise * rms / (np.std(noise) * (10 ** (noise_db / 20)))
        results.append(base64.b64encode(wave_bytes(samples, rate)).decode("ascii"))
    return results


def distance(reference, actual):
    row = list(range(len(actual) + 1))
    for index, character in enumerate(reference, 1):
        next_row = [index]
        for column, observed in enumerate(actual, 1):
            next_row.append(min(row[column] + 1, next_row[-1] + 1,
                                row[column - 1] + (character != observed)))
        row = next_row
    return row[-1]


def speech_evidence(args, report, trace, shaping, lines):
    lines = lines[1:2] if args.hybrid else (lines if args.interrupt else lines[:4])
    reference = list(map(compact, lines))
    actual = [compact(event["data"]["text"]) for event in trace["events"] if event["type"] == "user"
              and event["data"].get("input_mode") != "text"]
    samples = [(left, right, distance(left, right))
               for left, right in zip(reference, [*actual, *([""] * len(reference))])]
    return {
        "noise_snr_db": args.noise_db, "weak_network": shaping, "interrupt_requested": args.interrupt,
        "mixed_input": args.hybrid,
        "expected_utterances": len(reference), "transcribed_utterances": len(actual),
        "samples": [{"reference": left, "asr": right, "char_errors": edits} for left, right, edits in samples],
        "cer": round((sum(result[2] for result in samples) + sum(map(len, actual[len(reference):])))
                     / sum(map(len, reference)), 4),
        "complete": report["complete"], "severe_output": report["severe_output"],
        "audio": report["audio"],
    }


def voice_turns(page, asset, interrupt):
    heard = page.locator("#call-status")
    assistant = page.locator(".msg.assistant").last
    expect(heard).to_have_text("等待商家回应", timeout=18000)
    page.evaluate("audio => window.feedVoice(audio)", asset[0])
    expect(assistant).to_contain_text("方便了解", timeout=13000)
    expect(heard).to_have_text("等待商家回应", timeout=14000)
    page.evaluate("audio => window.feedVoice(audio)", asset[1])
    expect(assistant).to_contain_text("这次驳回", timeout=13000)
    if interrupt:
        expect(heard).to_have_text("正在播报", timeout=9000)
        page.wait_for_timeout(600)
    else:
        expect(heard).to_have_text("等待商家回应", timeout=20000)
    page.evaluate("audio => window.feedVoice(audio)", asset[2])
    expect(assistant).to_contain_text("位置上传或替换材料", timeout=18000)
    expect(heard).to_have_text("等待商家回应", timeout=16000)
    page.evaluate("audio => window.feedVoice(audio)", asset[3])
    if interrupt:
        expect(assistant).to_contain_text("这次驳回", timeout=14000)
        expect(heard).to_have_text("等待商家回应", timeout=22000)
        page.evaluate("audio => window.feedVoice(audio)", asset[4])


def hybrid_turns(page, asset):
    heard = page.locator("#call-status")
    assistant = page.locator(".msg.assistant").last
    microphone = page.locator("#microphone")
    box = page.locator("#message-input")

    def audible():
        page.wait_for_function("""() => {
            const remote = document.querySelector('#remote');
            if (!remote.srcObject || remote.paused || remote.muted) return false;
            if (!window.outputProbe) {
              const audio = new AudioContext();
              const meter = audio.createAnalyser();
              audio.createMediaStreamSource(remote.srcObject).connect(meter);
              audio.resume();
              window.outputProbe = {meter, data: new Float32Array(meter.fftSize)};
            }
            const {meter, data} = window.outputProbe;
            meter.getFloatTimeDomainData(data);
            return data.some(sample => Math.abs(sample) > .035);
        }""", timeout=15000)

    def type_reply(text):
        box.fill(text)
        box.press("Enter")

    expect(assistant).to_contain_text("京东商家资质审核的智能语音助手")
    expect(assistant).not_to_contain_text("这是一通比赛模拟通话")
    expect(microphone).to_have_text("开启麦克风")
    audible()
    assert page.evaluate("window.micRequests") == 0
    type_reply(LINES[0])
    expect(assistant).to_contain_text("方便了解", timeout=18000)
    expect(heard).to_have_text("正在播报", timeout=10000)
    audible()
    microphone.click()
    expect(microphone).to_have_text("关闭麦克风")
    audible()
    expect(heard).to_have_text("等待商家回应", timeout=14000)
    page.evaluate("audio => window.feedVoice(audio)", asset[1])
    expect(assistant).to_contain_text("这次驳回", timeout=13000)
    expect(heard).to_have_text("正在播报", timeout=10000)
    audible()
    microphone.click()
    expect(microphone).to_have_text("开启麦克风")
    audible()
    type_reply(LINES[2])
    expect(assistant).to_contain_text("位置上传或替换材料", timeout=24000)
    expect(heard).to_have_text("正在播报", timeout=10000)
    audible()
    type_reply(LINES[3])
    expect(assistant).to_contain_text("已登记本次沟通结果", timeout=22000)
    expect(heard).to_have_text("正在播报", timeout=10000)
    audible()
    assert page.evaluate("window.micRequests") == 1


def run(args):
    browser_path = args.browser
    if not browser_path and os.name == "nt":
        chrome = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")
        edge = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
        browser_path = str(chrome if chrome.is_file() else edge)
    output = args.output
    output.mkdir(parents=True, exist_ok=True)
    recordings = output / "screen"
    lines = LINES.copy()
    if args.weak_network and args.hybrid:
        lines[1] = "可以，请说吧。"
    asset = fixtures(SpeechModels(engine=args.fixture_tts), args.noise_db, lines)
    errors = []
    with sync_playwright() as playwright, httpx.Client(base_url=args.url, trust_env=False, timeout=25) as client:
        status = client.get("/api/status").json()
        assert status["voice"], "服务端语音模型未就绪"
        if args.weak_network:
            before_network = client.get("/_test/media-shaping")
            assert before_network.status_code == 200, "请使用 scripts.media_test_server 进行弱网测试"
            before_network = before_network.json()
        flags = ["--autoplay-policy=no-user-gesture-required", "--disable-background-timer-throttling",
                 "--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding"]
        browser = playwright.chromium.launch(executable_path=browser_path, headless=True, args=flags)
        video = {"record_video_dir": str(recordings), "record_video_size": {"width": 1280, "height": 800}} if args.video else {}
        context = browser.new_context(viewport={"width": 1280, "height": 800}, **video)
        context.add_init_script(TEST_MICROPHONE)
        page = context.new_page()
        capture_time = time.monotonic()
        screen = page.video
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(args.url, wait_until="domcontentloaded")
        expect(page.locator("#readiness")).to_have_text("离线语音就绪", timeout=15000)
        page.locator("#record").check()
        if args.video:
            page.wait_for_timeout(1400)
        with page.expect_response(lambda res: res.url.endswith("/api/offer")) as offer:
            page.locator("#text-start" if args.hybrid else "#start").click()
        assert offer.value.ok, f"音频连接请求失败：HTTP {offer.value.status}"
        call_id = offer.value.json()["session_id"]
        voice_offset = time.monotonic() - capture_time
        if args.hybrid:
            hybrid_turns(page, asset)
        else:
            voice_turns(page, asset, args.interrupt)
        expect(page.locator("#receipt")).to_be_visible(timeout=16000)
        expect(page.locator("#outcome")).to_have_text("已通知")
        expect(page.locator("#start")).to_be_enabled(timeout=18000)
        page.screenshot(path=output / "workbench.png")
        page.locator("#open-quality").click()
        expect(page.get_by_role("link", name="下载通话录音")).to_be_visible(timeout=10000)
        page.screenshot(path=output / "quality.png")
        if args.video:
            page.wait_for_timeout(3100)
        report = client.get(f"/api/calls/{call_id}/quality").json()
        trace = client.get(f"/api/calls/{call_id}/trace").json()
        shaping = None
        if args.weak_network:
            shaping = client.get("/_test/media-shaping").json()
            shaping["packets"] = {name: {key: value - before_network["packets"][name][key]
                                          for key, value in values.items()}
                                  for name, values in shaping["packets"].items()}
        audio = client.get(f"/api/calls/{call_id}/audio")
        audio.raise_for_status()
        context.close()
        if args.video:
            video_path = Path(screen.path())
        browser.close()

    evidence = speech_evidence(args, report, trace, shaping, lines)
    evidence["tts_engine"] = status.get("tts_engine")
    for name, item in (("quality.json", report), ("voice-trace.json", trace), ("speech-evidence.json", evidence)):
        (output / name).write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8")
    recording = output / "voice.wav"
    recording.write_bytes(audio.content)
    if args.video:
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        offset = max(0, voice_offset - .20)
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(video_path),
                        "-itsoffset", str(offset), "-i", str(recording), "-map", "0:v", "-map", "1:a",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "24", "-c:a", "aac", "-b:a", "112k",
                        "-movflags", "+faststart", str(output / "demo.mp4")], check=True)
    print(json.dumps({key: report[key] for key in ("session_id", "complete", "turns", "receipt", "audio")},
                     ensure_ascii=False, indent=2))
    assert report["complete"] and report["turns"] >= 3 and not report["severe_output"]
    assert report["audio"]["client_responses"] >= (1 if args.hybrid else 3)
    assert report["audio"]["client_p50"] <= 2000
    if status.get("tts_engine") == "matcha":
        assert report["audio"]["delivery_samples"] > 0 and report["audio"]["underrun_ms"] == 0
    if not args.hybrid:
        expected_turns = len(LINES) if args.interrupt else len(LINES) - 1
        assert report["turns"] == expected_turns, "同一句测试输入被切成多轮，或有语音输入丢失"
        assert report["audio"]["interruptions"] == int(args.interrupt), "打断次数与测试动作不一致"
    if args.weak_network:
        assert all(info["drop"] > 0 for info in shaping["packets"].values()), "未观察到双向注入丢包"
    if args.hybrid:
        inputs = [event["data"]["input_mode"] for event in trace["events"] if event["type"] == "user"]
        played = [event["data"]["turn"] for event in trace["events"] if event["type"] == "first_audio"]
        assert inputs == ["text", "voice", "text", "text"]
        assert played == [0, 1, 2, 3, 4] and not report["audio"]["interruptions"]
        print("同一会话完成文字、语音、文字输入，开场及四轮回复均收到音频。")
    assert not errors, errors


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--url", default="http://127.0.0.1:8765")
    cli.add_argument("--browser")
    cli.add_argument("--fixture-tts", choices=("matcha", "melo"), help="选择测试麦克风样音模型")
    cli.add_argument("--output", type=Path, default=Path("runtime/check"))
    cli.add_argument("--interrupt", action="store_true")
    cli.add_argument("--noise-db", type=float)
    cli.add_argument("--weak-network", action="store_true")
    cli.add_argument("--video", action="store_true")
    cli.add_argument("--hybrid", action="store_true")
    run(cli.parse_args())
