# 智语外呼

第二届北京市大学生“人工智能+”创新大赛赛题 2。系统在浏览器与本机服务之间建立 WebRTC 音频链路，使用模拟京东 POP 商家资审单据完成电话通知、京麦路由、回写、质检和知识迭代。

## 启动

需要 Python 3.13 x64，以及支持 WebRTC 的 Chrome 或 Edge。建议使用耳麦。

Windows 双击 `启动智语外呼.cmd`，或在项目根目录运行以下命令。首次启动会下载依赖和离线语音模型。已准备好的环境可加 `--no-fetch`。默认监听本机回环地址。

```powershell
python -X utf8 scripts/launch.py
```

打开 **http://127.0.0.1:8765**。启动日志显示应用就绪后再连接麦克风。Windows 如需安装并启用可选本地 Qwen 模型，首次运行加 `--fetch-llm`。若模型和运行器已在 `models/`，启动器会自动启用。指定的本机模型端口被占用时可用 `--model-port` 调整。

选择 `POP-001`，点击 **开始语音外呼**。依次说“我是商家负责人”“可以，请说明原因”“授权书在哪里上传”“我明白了，会补齐材料”。可在整改播报中直接开口追问。若整改说明被打断，系统重新说明后请再次确认。成功后可查看回执、质检、来源和原始事件。勾选“保存本次模拟录音”才会保存双声道 WAV。工作台仅使用模拟单据。

在 **自进化评测** 中点击“运行一轮 RSI”。页面展示开发、回归与封存集结果，并提供回滚和评测记录下载。想重复观测首轮改进时回滚至基线，再运行一轮。

## 验证

### 人工输入

在 **实时外呼** 中选择 `POP-001`，点击 **人工输入（文字）**。等待助手开场后，在右侧对话框逐句输入下列回复，每次按 Enter 或点击 **发送**，等助手回复后再继续。

1. 我是商家负责人。
2. 可以，请说明驳回原因。
3. 授权书在哪里上传？
4. 我明白了，会补齐材料。

预期结果为六个阶段全部完成，显示 **已通知** 和回执编号。点击 **查看本通质检** 可查看输入、答复和回写结果。也可以开一通新会话，第一句输入“不是负责人，打错了”，核对系统显示 **身份不符** 且未播报单据详情。

两个入口都建立语音通话并播放助手回复。**人工输入（文字）** 从麦克风关闭的状态开始，**开始语音外呼** 会尝试开启麦克风。通话中输入框始终可用，可点击 **开启麦克风** 或 **关闭麦克风** 在同一通电话里交替输入。播报过程中提交的文字会在当前播报发完后处理。需要打断时直接开口说话。质检日志保留每轮的输入方式，语音时延只统计麦克风轮次。

### 自动验证

```powershell
uv pip install --python .venv/Scripts/python.exe -r requirements-dev.txt
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m scripts.import_reference --source ..
.venv/Scripts/python.exe -m scripts.verify_voice --interrupt --output runtime/recheck
.venv/Scripts/python.exe -m scripts.verify_voice --hybrid --output runtime/hybrid-check
```

语音验收需要服务已启动。脚本在 Chrome 中注入合成麦克风音频，并经过正式页面和 WebRTC 通道。录像另加 `--video`，并先执行 `.venv/Scripts/python.exe -m playwright install ffmpeg`。正式比赛音频复测时请用现场耳麦和主办方测时工具。

弱网验收在两个窗口依次执行以下命令。

```powershell
.venv/Scripts/python.exe -m scripts.media_test_server
.venv/Scripts/python.exe -m scripts.verify_voice --url http://127.0.0.1:8766 --interrupt --weak-network --noise-db 12 --output runtime/release-shaped
```

## 第三方 AI 辅助问答

配置后，人工文字输入和 WebRTC 语音都能调用第三方模型理解追问。模型可以识别两个问题，选择知识主题和答复语气。系统从本地审核过的知识库取出对应答复并显示来源。身份核验、关单和回写由原有流程执行。

在 PowerShell 设置环境变量后启动。以下以 DeepSeek 的 Chat Completions 接口为例。使用其他兼容服务时，填写服务的基础 URL 和账号有权限使用的模型名。程序会追加 `/chat/completions`。 [DeepSeek 接口文档](https://api-docs.deepseek.com/api/create-chat-completion/)

```powershell
$env:ZHIYU_ANSWER_URL = 'https://api.deepseek.com'
$env:ZHIYU_ANSWER_MODEL = 'deepseek-flash'
$env:ZHIYU_ANSWER_TOKEN = [System.Net.NetworkCredential]::new('', (Read-Host 'API Key' -AsSecureString)).Password
python -X utf8 scripts/launch.py
```

| 可选变量 | 用途 |
|---|---|
| `ZHIYU_ANSWER_PROMPT` | 自定义 UTF-8 提示词文件路径。默认使用 `data/answer_prompt.md`。修改后重启。 |
| `ZHIYU_ANSWER_FORMAT` | 默认 `json_object`。支持严格结构化输出的服务可配置 `json_schema`。 |
| `ZHIYU_ANSWER_TIMEOUT` | 麦克风问答的等待预算，默认 1.2 秒。超时回退到本地知识或澄清。 |
| `ZHIYU_ANSWER_TEXT_TIMEOUT` | 文字问答的独立等待预算。不设置时沿用麦克风预算。 |

本项目也用 `https://api.srcbite.com/v1` 的 `gpt-6.1-sol` 验证了合成业务问句与 WebRTC 播报。两次模型请求分别约 5.24 秒和 3.55 秒。使用这个服务时，可将上述基础 URL 和模型名替换后，把 `ZHIYU_ANSWER_TEXT_TIMEOUT` 设为 `12`，让文字问答有充足时间等待模型。麦克风问答可沿用短预算，按赛场条件复测。API Key 由启动进程环境提供。

**人工测试**使用 `POP-001`，完成身份确认和原因说明后，在同一句输入「授权书在哪里上传？补齐后就能通过吗？」。命中两项知识时，回复会说明上传位置和审核结果仍须复核。采用模型建议的回复会标记 **AI 辅助** 并显示知识来源。同一句也可以通过麦克风说出。质检原始日志中的 `answer_assist` 会记录模型建议、最终知识编号、状态和耗时。接口超时、输出无效或知识不足时，系统使用本地已匹配答案，或按原流程澄清。

云模型验收请用人工编写的模拟问题。请求只包含经过本地字段处理的当前问题、通用候选主题和本地匹配提示。赛题原始资料、录音、完整转写、业务回写和知识答案留在本机。关闭 `ZHIYU_ANSWER_URL` 后重启即可回到本地问答。

## 容器与接口

Linux 可使用 `docker build -t zhiyu-outbound .` 和下面的命令。在同一主机打开浏览器。使用 host 网络是为了让浏览器可直接访问 ICE 媒体端口。Windows 推荐原生启动以减少容器媒体转发差异。

```sh
docker run --rm --network host -v zhiyu-evidence:/opt/zhiyu/runtime zhiyu-outbound python -m uvicorn zhiyu.app:api --host 127.0.0.1 --port 8765
```

`/docs` 列出本地 HTTP 接口，`/api/contracts/writeback` 输出回写 JSON Schema。业务接口目前由 `zhiyu.storage.MockTools` 提供本地实现。获得京东沙箱后，可在该边界接入名单、单据、工单和回写接口，并复用身份门禁和幂等键。

本地 FAQ 匹配与 RSI 语义抽检沿用 `ZHIYU_LLM_URL`、`ZHIYU_LLM_MODEL` 和 `ZHIYU_LLM_TOKEN`，期望 OpenAI 兼容的 Chat Completions 与 JSON Schema 输出。比赛环境优先使用本机模型。服务端运行数据保存在 `runtime/`，可用 `ZHIYU_RUNTIME` 另设目录。

## 提交材料

- `提交材料/技术方案书.md` 说明赛题映射、架构、评测方法和时延预算。
- `提交材料/自测报告.md` 记录实际语音与 RSI 测试结论，并链接原始记录。
- `提交材料/开源组件与模型清单.md` 记录版本、来源和许可证。
- `提交材料/演示视频.mp4` 录制浏览器的真实 WebRTC 通话。商家端语音来自标明的合成测试音频。
