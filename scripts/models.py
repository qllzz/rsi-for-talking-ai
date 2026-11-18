"""Fetch the pinned model files needed by the offline voice service."""
import argparse
import concurrent.futures
import hashlib
import os
import shutil
import tarfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "models"
ASR = "sherpa-onnx-streaming-zipformer-small-ctc-zh-int8-2025-04-01"
TTS = "https://huggingface.co/csukuangfj/vits-melo-tts-zh_en/resolve/a0d5c6a"
MATCHA = "matcha-icefall-zh-en"
VOCOS = "vocos-16khz-univ.onnx"
RELEASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models"
TTS_RELEASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models"
VOCODER_RELEASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download/vocoder-models"
LLAMA = "https://github.com/ggml-org/llama.cpp/releases/download/b12433"
GGUF = "https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/9217f5d"

SHA256 = {
    f"{MATCHA}.tar.bz2": "271b804af570400d3bcdcb53bf6e53cc9f75180ee763b9f13eb5eaf2b0d086ef",
    VOCOS: "b599142a1fb8ff03de3e84ac35ff537c619e56f4267a6fe894851a42844acf9e",
    "asr/model.int8.onnx": "68c9c943840f7d9cf3e8a4970ba50f404feb5277f611fa82b7e72267786fa84a",
    "asr/tokens.txt": "6fed8c6c248516f38e7faa19404b57413e8ce259f1cbc1fa4aebc86eac32fdfd",
    "asr/bbpe.model": "503204e0690eff065e30d0e01898c9ab06d0e6dc376a741eb6846198f95b2f82",
    "silero_vad.onnx": "9e2449e1087496d8d4caba907f23e0bd3f78d91fa552479bb9c23ac09cbb1fd6",
    "tts/model.int8.onnx": "f085f5079e05f039b800aeb542f5253c26a303211b0c6465d0d9387977855a63",
    "tts/lexicon.txt": "7236884b02435ac5d10cf69b4be40a61b45aa676b5300f0e412f185748fee528",
    "tts/tokens.txt": "d18664a7e12bd7ea1022ddaf951e534e136815016c5a809d6b64156bffb4369d",
    "qwen.gguf": "7671c0c304e6ce5a7fc577bcb12aba01e2c155cc2efd29b2213c95b18edaf6ed",
}

MATCHA_FILES = ("model-steps-3.onnx", "tokens.txt", "lexicon.txt", "date-zh.fst", "number-zh.fst",
                "phone-zh.fst", "espeak-ng-data/phondata")


def matcha():
    folder = ROOT / MATCHA
    marker = folder / ".archive-sha256"
    expected = SHA256[f"{MATCHA}.tar.bz2"]
    if (marker.is_file() and marker.read_text(encoding="ascii") == expected
            and all((folder / file).is_file() for file in MATCHA_FILES)):
        return
    archive = ROOT / f"{MATCHA}.tar.bz2"
    fetch(f"{TTS_RELEASE}/{archive.name}", archive, 75_000_000)
    with tarfile.open(archive, "r:bz2") as reader:
        members = reader.getmembers()
        if any(Path(member.name).parts[:1] != (MATCHA,) or not (member.isdir() or member.isfile())
               for member in members):
            raise ValueError("Matcha 压缩包的目录结构与预期不符")
        reader.extractall(ROOT, members=members, filter="data")
    if not all((folder / file).is_file() for file in MATCHA_FILES):
        raise ValueError("Matcha 模型缺少运行所需的文件")
    marker.write_text(expected, encoding="ascii")
    print(f"{folder.name} 已解包并校验", flush=True)


def matches(path, expected):
    if not path.is_file():
        return False
    if expected is None:
        return True
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest() == expected


def fetch(url, path, minimum):
    expected = SHA256.get(path.relative_to(ROOT).as_posix())
    if matches(path, expected) and path.stat().st_size >= minimum:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "Zhiyu-Research/1.0"})
    with urllib.request.urlopen(request, timeout=45) as source, temp.open("wb") as target:
        shutil.copyfileobj(source, target, 1024 * 1024)
    size = temp.stat().st_size
    if size < minimum:
        raise ValueError(f"{path.name} 只下载了 {size} 字节")
    if not matches(temp, expected):
        raise ValueError(f"{path.name} 的 SHA-256 与已核验资产不一致")
    os.replace(temp, path)
    print(f"{path.relative_to(ROOT)}  {size:,} B", flush=True)


def asr():
    paths = [ROOT / key for key in SHA256 if key.startswith("asr/")]
    if all(matches(path, SHA256[path.relative_to(ROOT).as_posix()]) for path in paths):
        return
    archive = ROOT / f"{ASR}.tar.bz2"
    fetch(f"{RELEASE}/{ASR}.tar.bz2", archive, 20_000_000)
    (ROOT / "asr").mkdir(exist_ok=True)
    with tarfile.open(archive, "r:bz2") as reader:
        for member in reader:
            path = Path(member.name)
            if member.isfile() and path.name in {"model.int8.onnx", "tokens.txt", "bbpe.model", "0.wav"}:
                with reader.extractfile(member) as source, (ROOT / "asr" / path.name).open("wb") as target:
                    shutil.copyfileobj(source, target)
    if not all(matches(path, SHA256[path.relative_to(ROOT).as_posix()]) for path in paths):
        raise ValueError("ASR 权重校验失败")


def llm():
    fetch(f"{GGUF}/qwen2.5-0.5b-instruct-q4_0.gguf", ROOT / "qwen.gguf", 400_000_000)


def llama_windows():
    target = ROOT / "llama"
    if (target / "llama-server.exe").is_file():
        return
    archive = ROOT / "llama-b12433-bin-win-cpu-x64.zip"
    fetch(f"{LLAMA}/{archive.name}", archive, 1_000_000)
    with zipfile.ZipFile(archive) as reader:
        target.mkdir(exist_ok=True)
        for item in reader.infolist():
            name = Path(item.filename).name
            if not item.is_dir() and name and name not in (".", ".."):
                with reader.open(item) as source, (target / name).open("wb") as output:
                    shutil.copyfileobj(source, output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--llm", action="store_true", help="also fetch Qwen and Windows llama.cpp")
    parser.add_argument("--only-llm", action="store_true")
    parser.add_argument("--tts-only", action="store_true", help="只更新语音合成模型")
    parser.add_argument("--tts-engine", choices=("matcha", "melo"),
                        default=os.environ.get("ZHIYU_TTS_ENGINE", "matcha"))
    args = parser.parse_args()
    if args.tts_engine not in {"matcha", "melo"}:
        parser.error("ZHIYU_TTS_ENGINE 必须为 matcha 或 melo")
    if args.tts_only and (args.llm or args.only_llm):
        parser.error("--tts-only 不能与大模型下载选项同时使用")
    files = {"model.int8.onnx": 50_000_000, "tokens.txt": 500, "lexicon.txt": 6_000_000,
             "date.fst": 50_000, "phone.fst": 80_000, "number.fst": 60_000,
             "new_heteronym.fst": 10_000, "LICENSE": 500}
    ROOT.mkdir(exist_ok=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        jobs = []
        if not args.only_llm and not args.tts_only:
            jobs = [pool.submit(asr), pool.submit(
                fetch, f"{RELEASE}/silero_vad.onnx", ROOT / "silero_vad.onnx", 100_000)]
        if not args.only_llm:
            if args.tts_engine == "matcha":
                jobs.append(pool.submit(matcha))
                jobs.append(pool.submit(fetch, f"{VOCODER_RELEASE}/{VOCOS}", ROOT / VOCOS, 50_000_000))
            else:
                jobs.extend(pool.submit(fetch, f"{TTS}/{file}", ROOT / "tts" / file, size)
                            for file, size in files.items())
        if args.llm or args.only_llm:
            jobs.append(pool.submit(llm))
            if os.name == "nt":
                jobs.append(pool.submit(llama_windows))
        for job in concurrent.futures.as_completed(jobs):
            job.result()


if __name__ == "__main__":
    main()
