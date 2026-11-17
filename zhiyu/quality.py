import math
from collections import Counter

from .dialog import FORBIDDEN, SENSITIVE, STAGES


def percentile(values, quantile):
    if not values:
        return None
    ordered = sorted(values)
    at = (len(ordered) - 1) * quantile
    lo, hi = math.floor(at), math.ceil(at)
    return round(ordered[lo] + (ordered[hi] - ordered[lo]) * (at - lo), 1)


def inspect(record):
    events = record["events"]
    of = lambda kind: [event["data"] for event in events if event["type"] == kind]
    stages = {data["stage"] for data in of("stage")}
    messages = of("assistant")
    compliance = of("compliance")
    server_ms = [item["ms"] for item in of("first_audio") if item["ms"] is not None]
    client_ms = [item["ms"] for item in of("client_audio")]
    interruption = of("interrupt")
    scopes = [stage for stage in STAGES if stage in stages]
    tools = of("tool")
    result = record["payload"]
    return {
        "session_id": record["id"], "case_id": record["case_id"], "revision": record["revision"],
        "mode": record["mode"], "receipt": record["receipt"], "ticket": record["ticket_id"],
        "result": result, "stages": scopes, "complete": bool(
            result and result["disposition"] == "notified" and len(scopes) == len(STAGES)),
        "turns": len(of("user")), "facts_with_source": sum(x["fact"] and bool(x["source"]) for x in messages),
        "severe_output": sum(bool(FORBIDDEN.search(x["text"]) or SENSITIVE.search(x["text"])
                                  or (x["fact"] and not x["verified"])) for x in messages),
        "compliance_intercepted": len(compliance),
        "failures": dict(Counter(x["reason"] for x in of("failure"))),
        "tools": tools,
        "audio": {"responses": len(server_ms), "server_p50": percentile(server_ms, .50),
                  "server_p95": percentile(server_ms, .95), "client_responses": len(client_ms),
                  "client_p50": percentile(client_ms, .50), "client_p95": percentile(client_ms, .95),
                  "interruptions": len(interruption),
                  "interrupt_stop_p95": percentile([x["stop_ms"] for x in interruption], .95),
                  "rtc_stats": (of("rtc_stats") or [None])[-1]},
        "transcript": [{"role": event["type"], **event["data"]} for event in events
                       if event["type"] in ("user", "assistant")],
    }
