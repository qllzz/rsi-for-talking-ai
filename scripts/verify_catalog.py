"""Exercise every organizer rule through the running business and quality APIs."""

import argparse
from collections import Counter
from contextlib import suppress
import json
from pathlib import Path

import httpx


SOURCE_ROWS = set(range(13, 81))
EXPECTED_ROUTES = {"phone": 54, "jingmai": 14}
PHONE_STAGES = ["opening", "identity", "needs", "offer", "confirm", "writeback"]


class VerificationError(ValueError):
    pass


def require(condition, detail):
    if not condition:
        raise VerificationError(detail)


def request(client, path, body=None, expected_status=None):
    try:
        response = client.get(path) if body is None else client.post(path, json=body)
        accepted = response.is_success if expected_status is None else response.status_code == expected_status
        require(accepted, f"{path}: HTTP {response.status_code}")
        return response.json()
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        raise VerificationError(f"{path}: {type(exc).__name__}") from None


def reference_cases(client):
    catalog = request(client, "/api/status").get("catalog", {})
    require(catalog.get("ready") is True, "原表规则库未就绪")
    require(
        (catalog.get("total"), catalog.get("phone"), catalog.get("jingmai")) == (68, 54, 14),
        "状态接口的原表规则总数或渠道数错误",
    )
    cases = request(client, "/api/cases")
    require(len({case["id"] for case in cases}) == len(cases), "单据编号重复")
    reference = [case for case in cases if case.get("source_kind") == "reference"]
    rows = [case["source_row"] for case in reference]
    require(len(rows) == len(SOURCE_ROWS) and set(rows) == SOURCE_ROWS, "原表行缺失或重复")
    require(Counter(case["source_route"] for case in reference) == EXPECTED_ROUTES, "原表渠道数错误")
    require(
        all(
            len(case["categories"]) == 3 and all(
                isinstance(category, str) and category.strip() for category in case["categories"]
            )
            for case in reference
        ),
        "三级分类有缺项",
    )
    return sorted(reference, key=lambda case: case["source_row"])


def verify_case(client, case, evidence):
    require(case["route"] == case["source_route"], "标准单据未按原表渠道处理")
    start = request(client, "/api/text", {"case_id": case["id"]})
    session = start["session_id"]
    evidence["session_id"] = session
    expected_stages = ["writeback"]
    expected_disposition = "jingmai"
    if case["source_route"] == "phone":
        require(
            start["state"] == "identity" and start["intent"] == "opening" and not start["fact"],
            "电话未先开场核验，或提前播报业务信息",
        )
        turn = f"/api/text/{session}/turn"
        identity = request(client, turn, {"text": "我是这家店的负责人"})
        require(identity["state"] == "needs" and identity["verified"] and not identity["fact"], "身份核验失败")
        offer = request(client, turn, {"text": "可以，请说明驳回原因和整改方案"})
        require(
            offer["state"] == "confirm" and offer["intent"] == "offer"
            and offer["fact"] and offer["source"] and offer["text"].strip(),
            "缺少带来源的整改说明，或业务话术被拦截",
        )
        require("!" + str(case["source_row"]) in offer["source"], "电话话术来源行错误")
        final = request(client, turn, {"text": "我明白了，会按要求处理"})
        expected_stages = PHONE_STAGES
        expected_disposition = "notified"
    else:
        final = start

    require(final["state"] == "done" and final["disposition"] == expected_disposition, "最终处理结果错误")
    require(bool(final["receipt"]), "没有收到回写回执")
    quality = request(client, f"/api/calls/{session}/quality")
    payload = quality["result"]
    is_phone = case["source_route"] == "phone"
    require(quality["case_id"] == case["id"] and quality["receipt"] == final["receipt"], "回写单据或回执不一致")
    require(payload["channel"] == case["source_route"] and payload["disposition"] == expected_disposition, "回写渠道或结果不一致")
    require(payload["identity_verified"] is is_phone and payload["acknowledged"] is is_phone, "回写的身份或确认状态错误")
    require(quality["stages"] == expected_stages and quality["complete"] is is_phone, "任务节点或闭环标记不完整")
    require(quality["turns"] == (3 if is_phone else 0), "质检轮次数错误")
    require(quality["facts_with_source"] >= 1 if is_phone else quality["facts_with_source"] == 0, "质检未记录业务事实的来源")
    require(quality["severe_output"] == 0 and quality["compliance_intercepted"] == 0, "质检发现违规或拦截")
    written = quality.get("written")
    require(bool(written and written.get("id", "").startswith("JM-")), "没有落库的京麦书面意见")
    require(written["case_id"] == case["id"], "书面意见单据错误")
    require(all(written.get(field) for field in ("reason", "action", "source")), "书面意见缺少原因、指引或来源")
    require("!" + str(case["source_row"]) in written["source"], "书面意见来源行错误")
    tools = {tool["name"] for tool in quality["tools"] if tool["ok"]}
    require({"lookup_list", "write_result"} <= tools, "缺少名单查询或回写工具记录")
    require(("lookup_application" in tools) is is_phone, "单据查询工具未遵守身份限制")
    evidence.update(
        disposition=expected_disposition,
        stages=quality["stages"],
        receipt=final["receipt"],
        written_id=written["id"],
        turns=quality["turns"],
        severe_output=quality["severe_output"],
    )


def verify_callback(client):
    case_id = "POP-005"
    first = request(client, "/api/text", {"case_id": case_id})["session_id"]
    missed = request(client, f"/api/text/{first}/terminal", {"command": "no_answer"})
    ticket = missed.get("ticket")
    require(missed["disposition"] == "callback" and bool(ticket), "第二次提交未接时缺少回联工单")

    def status():
        items = request(client, "/api/work-items")
        return next((item["status"] for item in items["tickets"] if item["id"] == ticket), None)

    require(status() == "pending", "回联工单未出现在待办中")
    args = {"case_id": case_id, "followup_ticket": ticket}
    request(client, "/api/text", {**args, "case_id": "POP-001"}, expected_status=409)
    abandoned = request(client, "/api/text", args)
    require(abandoned["state"] == "identity" and status() == "in_progress", "回联未按电话流程领取")
    request(client, "/api/text", args, expected_status=409)
    request(client, f"/api/text/{abandoned['session_id']}/terminal", {"command": "hangup"})
    require(status() == "pending", "主动结束后回联工单没有恢复待办")

    second = request(client, "/api/text", args)["session_id"]
    closed = request(client, f"/api/text/{second}/terminal", {"command": "no_answer"})
    report = request(client, f"/api/calls/{second}/quality")
    require(closed["disposition"] == "jingmai" and bool(closed.get("written")), "回联未接后未完成京麦书面关单")
    require(report["followup_ticket"] == ticket and status() is None, "回联记录没有关联或关闭工单")
    request(client, "/api/text", args, expected_status=409)
    return {
        "source_session": first, "followup_session": second, "ticket_id": ticket,
        "receipt": closed["receipt"], "written_id": closed["written"],
    }


def verify_handoff(client):
    started = request(client, "/api/text", {"case_id": "POP-001"})
    session = started["session_id"]
    turn = f"/api/text/{session}/turn"
    request(client, turn, {"text": "我是这家店的负责人"})
    handed = request(client, turn, {"text": "请转人工客服"})
    ticket = handed["ticket"]
    require(handed["disposition"] == "handoff" and bool(ticket), "没有登记人工复核工单")
    quality = f"/api/calls/{session}/quality"
    require(request(client, quality)["handoff_status"] == "pending", "人工复核状态未归档")
    resolve = f"/api/work-items/{ticket}/resolve"
    resolved = request(client, resolve, {})
    require(resolved["status"] == "resolved" and request(client, resolve, {}) == resolved, "人工复核处理未幂等落库")
    items = request(client, "/api/work-items")
    require(all(item["id"] != ticket for item in items["tickets"]), "已处理的人工复核仍出现在待办")
    require(request(client, quality)["handoff_status"] == "resolved", "质检未显示已处理的人工复核")
    return {"session_id": session, "ticket_id": ticket, "status": "resolved"}


def verify(client):
    evidence = {"entry_point": "text_api", "catalog_ready": False, "verified": 0, "failures": [], "rows": []}
    try:
        cases = reference_cases(client)
    except (VerificationError, KeyError, TypeError) as exc:
        evidence["failures"].append({"source_row": None, "check": str(exc)})
        return evidence
    evidence["catalog_ready"] = True
    for case in cases:
        row = {key: case[key] for key in ("id", "source_row", "source_route")}
        try:
            verify_case(client, case, row)
        except (VerificationError, KeyError, TypeError) as exc:
            row["passed"] = False
            evidence["failures"].append({"source_row": case["source_row"], "check": str(exc)})
            if row.get("session_id"):
                with suppress(VerificationError):
                    request(client, "/api/text/" + row["session_id"] + "/terminal", {"command": "hangup"})
        else:
            row["passed"] = True
            evidence["verified"] += 1
        evidence["rows"].append(row)
    for name, check in (("callback", verify_callback), ("handoff", verify_handoff)):
        try:
            evidence[name + "_workflow"] = check(client)
        except (VerificationError, KeyError, TypeError) as exc:
            evidence["failures"].append({"source_row": None, "workflow": name, "check": str(exc)})
    return evidence


def run(url, output):
    with httpx.Client(base_url=url, timeout=30.0, trust_env=False) as client:
        evidence = verify(client)
    output.mkdir(parents=True, exist_ok=True)
    report = output / "catalog-coverage.json"
    report.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"原表场景流程验收 {evidence['verified']}/68，失败 {len(evidence['failures'])} 项。报告 {report}")
    return not evidence["failures"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--output", default=Path("runtime/catalog-evidence"), type=Path)
    args = parser.parse_args()
    raise SystemExit(0 if run(args.url, args.output) else 1)
