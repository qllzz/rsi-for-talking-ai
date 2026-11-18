"""Package source, assessment evidence and the media submission."""
import argparse
import json
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_TOP = (".dockerignore", ".gitignore", "Dockerfile", "README.md", "requirements.txt",
              "requirements-dev.txt", "启动智语外呼.cmd")


def read(folder, filename):
    return json.loads((folder / filename).read_text(encoding="utf-8"))


def label(run, key):
    entry = run["summary"][key]
    return f"{entry['passed']} / {entry['count']}"


def report(clean, degraded, api, hybrid=False, catalog=None):
    rsi = api["rsi"]
    before = rsi["summary"]["holdout_before"]
    after = rsi["summary"]["holdout_after"]
    c = clean["audio"]
    w = degraded["audio"]
    shaping = degraded["weak_network"]
    up = shaping["packets"]["uplink"]
    down = shaping["packets"]["downlink"]
    weak_title = "12 dB 合成噪声与弱网混合输入" if degraded.get("mixed_input") else "12 dB 合成噪声与模拟弱网"
    weak_stop = f"{w['interrupt_stop_p95']} ms" if w["interrupt_stop_p95"] is not None else "—"
    hybrid_evidence = (
        "浏览器实测在同一会话先用文字输入，再开启麦克风回复，关闭麦克风后继续文字输入。"
        "开场和四轮回复均收听到音频，六个业务阶段及回写均完成。"
        " [混合输入原始记录](证据/混合输入-原始记录.json) · [通话录音](证据/混合输入.wav)\n\n"
        if hybrid else ""
    )
    catalog_evidence = ""
    if catalog:
        rows = catalog["rows"]
        phone_pass = sum(row["passed"] and row["source_route"] == "phone" for row in rows)
        jingmai_pass = sum(row["passed"] and row["source_route"] == "jingmai" for row in rows)
        catalog_evidence = (
            "## 原表规则全量验收\n\n"
            f"本地原表加载并逐条执行 {catalog['verified']} / {len(rows)} 条。"
            f"电话 {phone_pass} 条通过身份、整改说明、确认、关单和书面回写。"
            f"京麦 {jingmai_pass} 条通过书面关单和回执核对。"
            f"已通过的场景中严重输出违规为 {sum(row.get('severe_output', 0) for row in rows)} 条。"
            "报告还核对回联工单领取、释放、回联未接转京麦及人工复核处理。"
            "逐条验收从业务 API 运行，WebRTC 音频另见语音测试。"
            " [原表覆盖记录](证据/原表68条-覆盖记录.json)\n\n"
        )
    model = rsi.get("judge_model")
    judge_evidence = (
        f"{model} 对封存集 FAQ 类别抽样，前后得到 {before['judge_checks']} 和 {after['judge_checks']}，供语义复核。"
        if model else "本轮验收采用固定的规则判定，语义裁判模型未启用。"
    )
    return f"""# 智语外呼自测报告

## 环境与口径

在本机 Windows x64、Python 3.13 和 Chrome 上运行。语音服务为本地 CPU 推理。浏览器用合成测试音频作为麦克风输入，经过 WebRTC、VAD、ASR、对话、TTS 和回写。语音结果的统计单位是有效响应轮次。该样本用于检查功能与端到端时延。

## 语音与业务闭环

| 项目 | 本机正常传输 | {weak_title} |
|---|---:|---:|
| 完整六段闭环 | {'通过' if clean['complete'] else '未通过'} | {'通过' if degraded['complete'] else '未通过'} |
| 语音输入与转写轮次 | {clean['expected_utterances']} / {clean['transcribed_utterances']} | {degraded['expected_utterances']} / {degraded['transcribed_utterances']} |
| 浏览器响应有效样本 | {c['client_responses']} | {w['client_responses']} |
| 浏览器播放响应 P50 | {c['client_p50']} ms | {w['client_p50']} ms |
| 浏览器播放响应 P95 | {c['client_p95']} ms | {w['client_p95']} ms |
| 服务端首次出包 P50 | {c['server_p50']} ms | {w['server_p50']} ms |
| 识别字符错误率 | {clean['cer']:.1%} | {degraded['cer']:.1%} |
| 打断次数 | {c['interruptions']} | {w['interruptions']} |
| VAD 估算开口至服务端停止排队 P95 | {c['interrupt_stop_p95']} ms | {weak_stop} |
| Chrome 下行 RTP 丢包数 | {c['rtc_stats']['loss']} | {w['rtc_stats']['loss']} |
| 受控上行丢包 / 总包 | — | {up['drop']} / {up['total']} |
| 受控下行丢包 / 总包 | — | {down['drop']} / {down['total']} |
| 严重输出违规 | {clean['severe_output']} | {degraded['severe_output']} |

模拟弱网由独立回环测试进程在 aiortc 媒体传输层施加。RTP 上下行延迟均值 {shaping['delay_ms']} ms，标准差 {shaping['stdev_ms']} ms，随机丢包概率 {shaping['target_loss']:.0%}。信令与控制通道保持畅通。上下行数字是实际注入计数，Chrome 指标为浏览器报告。识别字符错误率对每句标注和识别文本去掉标点后按编辑距离计算，样本量与声源类型不足以推断总体识别质量。

两轮均检查身份、驳回原因、整改动作、可追溯追问、用户确认和结构化回执。正常传输的打断测试在整改播报期间插入商家追问，系统回答后再次说明未播完的整改内容，再等待确认。API 另验证了京麦路由、二次未接预约、身份不符和隐私诱导拦截。SQLite 并发测试验证同键同参数的重复回写返回同一回执，冲突参数会被拒绝。

{catalog_evidence}## 人工复测

在工作台选择 `POP-001`，点击 **人工输入（文字）** 即可收听开场。展开 **人工测试参考回复**，逐句输入，每句按 Enter 或点击 **发送**。通话中可开启麦克风、说出下一句，再关闭麦克风用键盘继续，开场及每轮回复均会语音播报。结束后检查六个阶段、回执和质检记录。身份异常可另开一通，第一句输入“不是负责人，打错了”，核对 **身份不符** 和单据详情的披露情况。麦克风轮次用于统计播放时延。

{hybrid_evidence}启用第三方模型后，可在身份确认及原因说明后问「授权书在哪里上传？补齐后就能通过吗？」。在页面检查所用知识来源，在原始日志中检查 `answer_assist` 事件。连接与提示词配置见源码包的 README。上表的语音时延数据使用离线验收配置，现场接入云模型时需按实际配置复测。

## RSI 对比

| 样本组 | 迭代前 | 迭代后 |
|---|---:|---:|
| 开发 | {label(rsi, 'dev_before')} | {label(rsi, 'dev_after')} |
| 回归 | {label(rsi, 'regression_before')} | {label(rsi, 'regression_after')} |
| 封存 | {label(rsi, 'holdout_before')} | {label(rsi, 'holdout_after')} |

固定规则版本为 `{rsi['criteria']}`，随机种子为 `{rsi['seed']}`。活跃候选为 `{rsi['id']}`，随后通过接口回滚至基线。封存集迭代前后使用同一用例哈希 `{before['dataset_sha256']}`。本轮自动挖掘的补丁为 `{json.dumps(rsi['patch'], ensure_ascii=False)}`。回归退化 {len(rsi['regressions'])} 条。{judge_evidence}

## 原始记录与核验范围

- [正常链路质检及时延](证据/正常链路-语音指标.json) · [完整事件](证据/正常链路-原始记录.json) · [通话录音](证据/正常链路.wav)
- [弱网加噪质检及时延](证据/弱网加噪-语音指标.json) · [完整事件](证据/弱网加噪-原始记录.json) · [通话录音](证据/弱网加噪.wav)
- [RSI 逐用例日志](证据/RSI-原始记录.json) · [业务接口验收与回滚](证据/业务接口验收.json)

测量使用浏览器音频能量阈值本地估算播放时延。合成信号经浏览器媒体轨道传输，耳麦和实际室内声学条件仍需赛场复测。当前没有独立统计 VAD 准确率、意图准确率或人工听评的自然度。官方 Mock、未公开评测和统一时延工具待赛方开放。Dockerfile 随源码提供，本机验收走 Windows 原生启动。
"""


def package(normal, weak, backend, output, hybrid=None, catalog=None):
    evidence = output / "证据"
    evidence.mkdir(parents=True, exist_ok=True)
    for origin, label_text in [(normal, "正常链路"), (weak, "弱网加噪")]:
        for source, target in [("speech-evidence.json", f"{label_text}-语音指标.json"),
                               ("voice-trace.json", f"{label_text}-原始记录.json"),
                               ("voice.wav", f"{label_text}.wav")]:
            shutil.copyfile(origin / source, evidence / target)
    if hybrid:
        shutil.copyfile(hybrid / "voice-trace.json", evidence / "混合输入-原始记录.json")
        shutil.copyfile(hybrid / "voice.wav", evidence / "混合输入.wav")
    catalog_report = read(catalog, "catalog-coverage.json") if catalog else None
    if catalog_report:
        if catalog_report["verified"] != 68 or catalog_report["failures"]:
            raise ValueError("原表全量验收未通过，请先检查逐条覆盖报告。")
        shutil.copyfile(catalog / "catalog-coverage.json", evidence / "原表68条-覆盖记录.json")
    shutil.copyfile(backend / "rsi-trace.json", evidence / "RSI-原始记录.json")
    shutil.copyfile(backend / "api-summary.json", evidence / "业务接口验收.json")
    shutil.copyfile(normal / "demo.mp4", output / "演示视频.mp4")
    for name in ("技术方案书.md", "开源组件与模型清单.md"):
        shutil.copyfile(ROOT / "docs" / name, output / name)
    text = report(read(normal, "speech-evidence.json"), read(weak, "speech-evidence.json"),
                  read(backend, "api-summary.json"), hybrid=bool(hybrid), catalog=catalog_report)
    (output / "自测报告.md").write_text(text, encoding="utf-8")
    sources = [ROOT / name for name in SOURCE_TOP]
    for folder in ("zhiyu", "data", "web", "scripts", "tests", "docs"):
        sources.extend(file for file in (ROOT / folder).rglob("*")
                       if file.is_file() and "__pycache__" not in file.parts)
    with zipfile.ZipFile(output / "智语外呼-源代码.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for file in sorted(sources):
            archive.write(file, file.relative_to(ROOT).as_posix())
    print(output.resolve())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--normal", type=Path, default=Path("runtime/release-demo"))
    parser.add_argument("--weak", type=Path, default=Path("runtime/release-shaped"))
    parser.add_argument("--backend", type=Path, default=Path("runtime/api-evidence"))
    parser.add_argument("--hybrid", type=Path)
    parser.add_argument("--catalog", type=Path, help="包含原表 68 条覆盖记录的验收目录")
    parser.add_argument("--output", type=Path, default=Path("提交材料"))
    args = parser.parse_args()
    package(args.normal, args.weak, args.backend, args.output, args.hybrid, args.catalog)
