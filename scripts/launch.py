"""One-command local launch for the competition sandbox."""
import argparse
import os
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENV = ROOT / ".venv"
PYTHON = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def prepare(no_fetch, model):
    if not PYTHON.is_file():
        if no_fetch:
            raise RuntimeError("缺少 .venv。先运行不带 --no-fetch 的启动脚本。")
        if shutil.which("uv"):
            subprocess.run(["uv", "venv", "--python", sys.executable, str(VENV)], check=True)
        else:
            venv.EnvBuilder(with_pip=True).create(VENV)
    if subprocess.run([str(PYTHON), "-c", "import aiortc, sherpa_onnx, fastapi, uvicorn, openpyxl"],
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
        if no_fetch:
            raise RuntimeError("缺少依赖。先安装 requirements.txt。")
        uv = shutil.which("uv")
        if uv:
            os.environ.setdefault("UV_CACHE_DIR", str(ROOT / ".cache" / "uv"))
            installer = [uv, "pip", "install", "--python", str(PYTHON)]
        else:
            installer = [str(PYTHON), "-m", "pip", "install"]
        subprocess.run([*installer, "-r", "requirements.txt"], check=True)
    if not no_fetch:
        subprocess.run([str(PYTHON), "scripts/models.py", *(["--llm"] if model else [])], check=True)


def remote_judge_enabled(env):
    check = (
        "from urllib.parse import urlsplit\n"
        "from zhiyu.connections import load_connection\n"
        "profile = load_connection('judge')\n"
        "loopback = {'127.0.0.1', '::1', 'localhost'}\n"
        "print(int(bool(profile and profile.enabled and urlsplit(profile.url).hostname not in loopback)))\n"
    )
    # The launcher only needs the standard library before the virtual environment is prepared.
    result = subprocess.run([str(PYTHON), "-c", check], env=env, check=True, capture_output=True, text=True)
    return result.stdout.strip() == "1"


def local_model(env, port):
    binary = ROOT / "models" / "llama" / ("llama-server.exe" if os.name == "nt" else "llama-server")
    weights = ROOT / "models" / "qwen.gguf"
    if not binary.exists() and os.name != "nt" and shutil.which("llama-server"):
        binary = Path(shutil.which("llama-server"))
    if not binary.is_file() or not weights.is_file():
        return None
    logs = ROOT / "runtime"
    logs.mkdir(exist_ok=True)
    auth_file = logs / "llama-key"
    auth_file.write_text(secrets.token_urlsafe(32), encoding="ascii")
    args = [str(binary), "-m", "models/qwen.gguf", "--alias", "qwen-local", "--ctx-size", "1536",
            "--parallel", "1", "--threads", "6", "--host", "127.0.0.1", "--port", str(port),
            "--n-gpu-layers", "0", "--api-key-file", "runtime/llama-key"]
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    with (logs / "llama.log").open("wb") as log:
        server = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT, creationflags=flags)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    token = auth_file.read_text(encoding="ascii")
    address = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 18
    while time.monotonic() < deadline and server.poll() is None:
        try:
            with opener.open(address + "/health", timeout=.8) as response:
                if response.status == 200:
                    env.update(ZHIYU_LLM_URL=address + "/v1", ZHIYU_LLM_TOKEN=token)
                    return server
        except (TimeoutError, urllib.error.URLError):
            time.sleep(.25)
    if server.poll() is None:
        server.terminate()
    print("Qwen 未能启动，将使用可复现的本地检索。详情见 runtime/llama.log。", flush=True)
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default=8765, type=int)
    parser.add_argument("--model-port", default=8087, type=int)
    parser.add_argument("--fetch-llm", action="store_true")
    parser.add_argument("--no-llm", action="store_true")
    parser.add_argument("--no-fetch", action="store_true")
    args = parser.parse_args()
    os.chdir(ROOT)
    env = os.environ.copy()
    prepare(args.no_fetch, args.fetch_llm)
    model = None
    if not args.no_llm and not env.get("ZHIYU_LLM_URL") and not remote_judge_enabled(env):
        model = local_model(env, args.model_port)
    try:
        print(f"在浏览器打开 http://127.0.0.1:{args.port}", flush=True)
        return subprocess.call([str(PYTHON), "-X", "utf8", "-m", "uvicorn", "zhiyu.app:api",
                                "--host", "127.0.0.1", "--port", str(args.port)], env=env)
    finally:
        if model and model.poll() is None:
            model.terminate()
            model.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
