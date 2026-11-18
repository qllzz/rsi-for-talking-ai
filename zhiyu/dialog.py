import re
import sqlite3
import time
from uuid import uuid4

from .answer import OPENINGS
from .knowledge import compact

STAGES = ("opening", "identity", "needs", "offer", "confirm", "writeback")
SENSITIVE = re.compile(r"(?<!\d)\d{7,}(?!\d)|[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}")
FORBIDDEN = re.compile(r"保证.{0,5}通过|我.{0,2}保证|百分之百.{0,3}通过|包过|肯定.{0,3}通过|"
                       r"报.{0,5}(验证码|身份证号码|银行卡号)|下发.{0,2}验证码|加我微信|私下转账|傻[瓜逼]|滚开")
PROBE = re.compile(r"(忽略|跳过).{0,10}(指令|规则|核验)|系统提示词|别人.{0,8}(信息|手机号)|"
                   r"(完整|全部).{0,5}(手机|电话|证件|身份证).{0,3}(号码|号)|给我.{0,8}验证码")
WRONG = re.compile(r"打错|找错|不是.{0,7}(负责人|商家|这里)|你找谁|不认识|不方便.{0,3}核实|不.{0,3}是我")
IDENTITY = re.compile(
    r"^(?:喂|你好|您好|嗯|恩|啊|哎|唉|诶|对|是的)*"
    r"(?:(?:我(?:本人)?|本人|我们)(?:就是|是)(?:这家店|该店|这家公司|该公司|本店|本公司|"
    r"商家|这家商家|这家店铺|该店铺|店铺|公司)?的?(?:负责人|店主)|"
    r"我是商家|对的|是的|对|没错|我就是|我负责|是我|是)"
    r"(?:您说吧?|你说吧?|请说|请讲|说吧|什么事|请问有什么事)?$"
)
DEFER = re.compile(r"晚点|回电|稍后再|没空|再打|不[太]?方便|过会儿")
ESCALATE = re.compile(r"转人工|接.{0,2}人工客服|找.{0,2}人工|人工客服$|我要.{0,2}投诉|投诉你|曝光|举报|^投诉$")
ANGRY = re.compile(r"太离谱|太麻烦|不合理|烦死|生气|烦人|着急|怎么又")
CONFIRM = re.compile(r"明白|清楚|知道了|好的|好$|会.{0,5}(补|改|提交)|"
                     r"听懂|我去(补|改)|我(补|改|提交)|就这么|嗯[，,。 ]*(行|对)")
REPEAT = re.compile(r"再说|没听清|重复|刚才.{0,4}什么|没听懂|刚刚|不[太]?清楚|不[太]?明白|没[太]?明白")
NEED = re.compile(r"原因|为什么|怎么|说吧|你说|可以|方便|什么|明白|[好行]|^对|[嗯恩]|材料|驳回")
QUESTION = re.compile(r"[？?]|吗|怎样|如何|怎么|为啥|哪里|在哪|哪个|哪一|什么|多久|多少|何时|"
                      r"会不会|能不能|能否|可不可以|是否|有没有|想问|想知道")
VERIFY_OK = "谢谢您的确认。现在方便了解这次资质申请被驳回的原因和整改步骤吗？"
NOTIFIED = "已登记本次沟通结果。感谢您的配合，再见。"


def offer_script(case):
    return f"这次驳回是因为{case.reason}。请您{case.action}。请问整改步骤清楚了吗？"


def voice_templates(knowledge):
    # Prewarm common replies and the stable first-line call cases. Other scenarios are
    # synthesized on demand and share the bounded phrase cache during calls.
    common_cases = {"POP-001", "POP-002", "POP-003", "POP-005", "POP-008"}
    return [
        knowledge.identity + knowledge.opening, VERIFY_OK, NOTIFIED,
        *filter(None, OPENINGS.values()),
        *(offer_script(case) for case in knowledge.cases.values() if case.id in common_cases and case.reason and case.action),
        *(entry.answer + "请问还有其他疑问吗？" for entry in knowledge.entries.values() if not entry.source_rows),
        "我只能协助本次资质审核事项，无法提供这些信息。",
        "为保护商家信息，请先确认您是否是这家商家的负责人。",
        "我暂时无法确认这个问题。您是想核对驳回原因、整改操作，还是重新提交的步骤？",
        "我理解您希望进一步核实，我会登记人工复核。",
        "理解您现在不方便，我会登记后续回电需求。",
        "明白，打扰您了。感谢接听，再见。",
    ]


def redact(text):
    return SENSITIVE.sub("【已脱敏】", text)


class Dialogue:
    def __init__(self, knowledge, tools, store, case_id, mode, resolver=None, session_id=None, *,
                 answer_assistant=None, followup_ticket=None):
        self.knowledge = knowledge
        self.tools = tools
        self.store = store
        self.case = knowledge.cases[case_id]
        self.resolver = resolver
        self.answer_assistant = answer_assistant
        self.id = session_id or uuid4().hex
        self.state = "idle"
        self.index = 0
        self.verified = False
        self.acknowledged = False
        self.offered = False
        self.need_confirmed = False
        self.disposition = None
        self.receipt = None
        self.ticket = None
        self.written = None
        self.followup_ticket = followup_ticket
        self.pending_result = None
        self.stages = []
        self.last_entry = None
        self.last_factual_reply = None
        self.unknown = 0
        self.identity_retries = 0
        self.opening_interrupted = False
        self.channel = "phone" if followup_ticket else self.case.channel()
        self.application = None
        store.begin(self.id, case_id, mode, knowledge.revision, followup_ticket=followup_ticket)

    def abandon(self):
        if self.followup_ticket:
            self.store.release_followup(self.id)

    def mark(self, *stages):
        for stage in stages:
            if stage not in self.stages:
                self.stages.append(stage)
                self.store.log(self.id, "stage", stage=stage)

    def tool(self, name, call, *args):
        try:
            data = call(*args)
        except (ValueError, KeyError, PermissionError, OSError, sqlite3.Error) as exc:
            self.store.log(self.id, "tool", name=name, ok=False, error=type(exc).__name__)
            raise
        self.store.log(self.id, "tool", name=name, ok=True)
        return data

    def reply(self, text, intent, source=None, reason=None, fact=False, answer_model=None):
        flagged = bool(FORBIDDEN.search(text) or SENSITIVE.search(text) or (fact and not self.verified))
        if flagged:
            self.store.log(self.id, "compliance", action="blocked", severity="severe",
                           reason="pre_speech_policy")
            text = "这项信息暂时无法通过电话提供，请通过京麦核实。"
            source, fact = None, False
            answer_model = None
        response = {"text": text, "intent": intent, "source": source, "reason": reason, "fact": fact,
                    "state": self.state, "turn": self.index, "stages": self.stages[:],
                    "verified": self.verified, "receipt": self.receipt, "ticket": self.ticket, "written": self.written,
                    "disposition": self.disposition}
        if answer_model:
            response["answer_model"] = answer_model
        self.store.log(self.id, "assistant", **response)
        return response

    def answer_question(self, text, entry, input_mode):
        entries = []
        opening = ""
        model = None
        if self.answer_assistant:
            clauses = re.split(r"[？?。！!，,；;]+", text)
            if sum(bool(clause.strip()) for clause in clauses) > 1:
                entries = [self.knowledge.retrieve(clause, self.case)
                           for clause in clauses if clause.strip()]
        known = list(dict.fromkeys(item.id for item in (*entries, entry) if item))[:2]
        topics = known
        if self.answer_assistant:
            start = time.monotonic()
            plan = self.answer_assistant.plan(text, self.case, self.knowledge.allowed_entries(self.case),
                                              entry.id if entry else None, input_mode=input_mode)
            if plan.status == "used":
                topics = list(dict.fromkeys([*known, *plan.topics]))[:2]
                opening = OPENINGS[plan.style]
                model = self.answer_assistant.model
            self.store.log(self.id, "answer_assist", turn=self.index, model=self.answer_assistant.model,
                           status=plan.status, topics=list(plan.topics), answer_topics=topics,
                           http_status=plan.http_status, elapsed_ms=round((time.monotonic() - start) * 1000, 1))
        if not topics:
            return None
        entries = [self.knowledge.entries[topic] for topic in topics]
        self.unknown = 0
        self.last_entry = entries[0].id
        answer = opening + "".join(item.answer for item in entries)
        source = " ｜ ".join(dict.fromkeys(item.source for item in entries))
        self.last_factual_reply = (answer, source)
        return self.reply(answer + "请问还有其他疑问吗？", f"faq:{self.last_entry}", source, fact=True,
                          answer_model=model)

    def start(self):
        if self.state != "idle":
            raise ValueError("会话已经开始")
        self.tool("lookup_list", self.tools.lookup_list, self.case.id)
        self.store.log(self.id, "contact_plan", channel=self.channel, submission_attempt=self.case.attempt,
                       contact_attempt=2 if self.followup_ticket else 1, followup_ticket=self.followup_ticket)
        if self.channel == "jingmai":
            return self.finish("jingmai", "该单据已登记京麦书面处理。")
        self.mark("opening")
        self.state = "identity"
        return self.reply(self.knowledge.identity + self.knowledge.opening, "opening")

    def interrupt(self, response_intent):
        if response_intent == "opening":
            self.opening_interrupted = True
        if response_intent == "offer":
            self.offered = False

    def offer(self):
        case = self.application
        if not case or not case.reason or not case.action:
            return self.finish("handoff", "这条单据需要人工核实，我会登记转接。")
        text = offer_script(case)
        if FORBIDDEN.search(text) or SENSITIVE.search(text):
            self.store.log(self.id, "compliance", action="blocked", severity="severe", reason="unsafe_case_content")
            return self.finish("handoff", "这条单据需要人工核实，我会登记转接。")
        self.mark("offer")
        self.offered = True
        self.state = "confirm"
        self.last_factual_reply = (text, case.source)
        return self.reply(text, "offer", case.source, fact=True)

    def finish(self, disposition, text):
        if self.state == "done":
            return self.reply("本次沟通结果已登记。", "done")
        if self.pending_result is None:
            self.pending_result = {
                "session_id": self.id, "case_id": self.case.id, "channel": self.channel,
                "disposition": disposition, "identity_verified": self.verified,
                "acknowledged": self.acknowledged, "turn_count": self.index,
                "revision": self.knowledge.revision,
            }
        disposition = self.pending_result["disposition"]
        try:
            self.receipt = self.tool("write_result", self.tools.write_result, self.pending_result)
            artifacts = self.tools.result_artifacts(self.id)
            self.ticket = artifacts["ticket"]
            self.written = artifacts["written"]
        except (ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
            self.state = "write_pending"
            self.store.log(self.id, "failure", reason="write_failed", error=type(exc).__name__)
            return self.reply("暂时无法确认登记结果，请稍后重试。", "write_pending", reason="tool")
        self.mark("writeback")
        self.state = "done"
        self.disposition = disposition
        return self.reply(text, disposition)

    def terminal(self, trigger):
        if self.state == "write_pending":
            return self.finish(self.pending_result["disposition"], "处理结果已登记。")
        if trigger == "no_answer" and not self.verified:
            if self.case.attempt == 2 and self.channel == "phone" and not self.followup_ticket:
                return self.finish("callback", "电话未接通，已登记预约再次联系。")
            return self.finish("jingmai", "电话未接通，已登记京麦站内处理。")
        return self.finish("ended", "本次通话已结束。")

    def handle(self, raw, *, input_mode=None):
        text = self.knowledge.transcript(redact(raw))[:400]
        key = compact(text)
        self.index += 1
        self.store.log(self.id, "user", turn=self.index, text=text, state=self.state,
                       **({"input_mode": input_mode} if input_mode else {}))
        if self.state == "done":
            return self.reply("本次沟通结果已登记。", "done")
        if self.state == "write_pending":
            return self.terminal("retry")
        if PROBE.search(key):
            self.store.log(self.id, "compliance", action="refused", severity="protected",
                           reason="untrusted_input")
            return self.reply("我只能协助本次资质审核事项，无法提供这些信息。", "protected")
        if WRONG.search(key):
            if self.verified:
                self.verified = False
                return self.finish("wrong_party", "收到您的更正。为保护业务信息，本次通话到此结束。")
            return self.finish("wrong_party", "明白，打扰您了。感谢接听，再见。")
        if ESCALATE.search(key):
            return self.finish("handoff", "我理解您希望进一步核实，我会登记人工复核。")
        if DEFER.search(key):
            return self.finish("callback", "理解您现在不方便，我会登记后续回电需求。")
        if self.state == "identity":
            self.identity_retries += 1
            if self.opening_interrupted:
                self.opening_interrupted = False
                return self.reply(self.knowledge.identity + self.knowledge.opening, "opening")
            if IDENTITY.search(key):
                try:
                    self.application = self.tool("lookup_application", self.tools.lookup_application, self.case.id, True)
                except (ValueError, KeyError, PermissionError, OSError, sqlite3.Error):
                    return self.finish("handoff", "我暂时无法核对这条单据，已为您登记人工复核。")
                self.verified = True
                self.mark("identity")
                self.state = "needs"
                return self.reply(VERIFY_OK, "verify")
            if self.identity_retries >= 3:
                return self.finish("ended", "暂时无法核实身份，感谢您的接听。")
            return self.reply("为保护商家信息，请先确认您是否是这家商家的负责人。", "verify_again")
        if self.state not in {"needs", "confirm"} or not self.verified:
            return self.reply("请先发起模拟通话。", "idle")
        if self.state == "needs":
            if ANGRY.search(key) or NEED.search(key):
                self.need_confirmed = True
                self.mark("needs")
                return self.offer()
            return self.reply("本次来电是为了说明整改事项，您方便现在听一下吗？", "clarify_need")
        if REPEAT.search(key):
            if not self.offered:
                return self.offer()
            if self.last_factual_reply:
                text, source = self.last_factual_reply
                return self.reply(text, "repeat", source, fact=True)
        if re.search(r"驳回.{0,4}原因|为什么.{0,4}驳回|^为什么$|怎么整改", key):
            return self.offer()
        if self.last_entry and re.search(r"那一步|上一步|刚才那项|这个步骤", key):
            entry = self.knowledge.entries[self.last_entry]
            return self.reply(entry.answer, f"faq:{entry.id}", entry.source, fact=True)
        if ANGRY.search(key):
            return self.reply("理解反复补交材料会给您添麻烦。您可以告诉我哪个步骤最不清楚，我帮您核对。",
                              "empathy")
        question = bool(QUESTION.search(text))
        resolver = self.resolver if question and not self.answer_assistant else None
        entry = self.knowledge.retrieve(text, self.case, resolver)
        confirmed = not question and bool(CONFIRM.search(key))
        if not confirmed and (entry or self.answer_assistant):
            answer = self.answer_question(text, entry, input_mode)
            if answer:
                return answer
        if confirmed:
            if not self.offered:
                return self.offer()
            self.acknowledged = True
            self.mark("confirm")
            return self.finish("notified", NOTIFIED)
        self.unknown += 1
        self.store.log(self.id, "failure", reason="knowledge_gap", turn=self.index, query=text)
        if self.unknown >= 3:
            return self.finish("handoff", "这个问题需要进一步核实，我为您登记人工处理。")
        return self.reply("我暂时无法确认这个问题。您是想核对驳回原因、整改操作，还是重新提交的步骤？",
                          "clarify", reason="knowledge_gap")
