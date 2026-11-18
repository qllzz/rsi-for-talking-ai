import re
import sqlite3
import time
from collections import deque
from uuid import uuid4

from .agent import SLOT, Fact
from .answer import OPENINGS
from .knowledge import DIALOGUE_POLICY, compact

STAGES = ("opening", "identity", "needs", "offer", "confirm", "writeback")
SENSITIVE = re.compile(r"(?<!\d)\d{7,}(?!\d)|[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}")
FORBIDDEN = re.compile(r"保证.{0,5}通过|我.{0,2}保证|百分之百.{0,3}通过|包过|肯定.{0,3}通过|"
                       r"报.{0,5}(验证码|身份证号码|银行卡号)|下发.{0,2}验证码|加我微信|私下转账|傻[瓜逼]|滚开")
PROBE = re.compile(r"(忽略|跳过).{0,10}(指令|规则|核验)|系统提示词|别人.{0,8}(信息|手机号)|"
                   r"(完整|全部).{0,5}(手机|电话|证件|身份证).{0,3}(号码|号)|给我.{0,8}验证码")
WRONG = re.compile(r"打错|找错|不是.{0,7}(负责人|商家|这里)|不认识|不.{0,3}是我")
IDENTITY = re.compile(
    r"^(?:喂|你好|您好|嗯|恩|啊|哎|唉|诶|对|是的)*"
    r"(?:(?:我(?:本人)?|本人|我们)(?:就是|是)(?:这家店|该店|这家公司|该公司|本店|本公司|"
    r"商家|这家商家|这家店铺|该店铺|店铺|公司)?的?(?:负责人|店主)|"
    r"我是商家|对的|是的|对|没错|我就是|我负责|是我|是)"
    r"(?:您说吧?|你说吧?|请说|请讲|说吧|什么事|请问有什么事)?$"
)
DEFER = re.compile(r"(?:晚点|稍后|过会儿|待会儿|改天).{0,5}(?:打|联系|说)|(?:请|麻烦|给我).{0,7}回电|"
                   r"(?:现在|这会儿)?.{0,3}(?:没空|不[太]?方便)(?:接听|说话|聊|听|通话|接电话|$)|再打[给过]?")
IDENTITY_ORIGIN = re.compile(r"你(?:们)?是[谁哪]|你找谁|什么(?:部门|电话)|哪里打(?:来|的)|诈骗|真假|核实来电")
ESCALATE = re.compile(r"转人工|接.{0,2}人工客服|找.{0,2}人工|人工客服$|我要.{0,2}投诉|投诉你|曝光|举报|^投诉$")
ANGRY = re.compile(r"太离谱|太麻烦|不合理|烦死|生气|烦人|着急|怎么又")
CONFIRM = re.compile(r"明白|清楚|知道了|好的|好$|会.{0,5}(补|改|提交)|"
                     r"听懂|我去(补|改)|我(补|改|提交)|就这么|嗯[，,。 ]*(行|对)")
REPEAT = re.compile(r"再说|没听清|重复|刚才.{0,4}什么|没听懂|刚刚|不[太]?清楚|不[太]?明白|没[太]?明白")
NEED = re.compile(r"原因|为什么|怎么|说吧|你说|可以|方便|什么|明白|[好行]|^对|[嗯恩]|材料|驳回")
QUESTION = re.compile(r"[？?]|吗|怎样|如何|怎么|为啥|哪里|在哪|哪个|哪一|什么|多久|多少|何时|"
                      r"会不会|能不能|能否|可不可以|是否|有没有|想问|想知道")
SELF_ATTESTATION = re.compile(r"^(?:(?:喂|你好|您好|嗯|恩|啊|对|是的)[，,。.!！\s]*)*(?:我(?:们)?(?:本人)?|本人)"
                             r"(?:就是|是|在负责|负责)(?:这家店|这个店|这个商家|这边|本店|本公司|其|店里|公司|店铺|商家)?"
                             r"的?(?:负责人|店主|老板|经营者|资质(?:申请|审核|提交)?|经营|审核)(?=$|[，,。.!！；;\s])")
IDENTITY_DOUBT = re.compile(r"不是|并非|不算|代运营|帮忙|转述|假装|假设|如果|把我当|他(?:说|是)|不确定|可能")
NEGATIVE_CONFIRMATION = re.compile(r"不.{0,4}(?:明白|清楚|理解|知道|懂)|没.{0,4}(?:听懂|明白|弄懂)|(?<!没)有.{0,3}(?:疑问|问题)")
FOLLOWUP_QUESTION = re.compile(r"[？?]|请问|想问|想知道|能不能|会不会|能否|可不可以|有没有|是否|要不要|[吗么呢][。！!\s]*$")
BEFORE_IDENTITY = re.compile(r"授权书|商标|驳回原因是|需要补(?:交|齐)|已经(?:通过|驳回)|申请存在")
UNSUPPORTED_OUTPUT = re.compile(r"https?://|www\.|\d|```|<[^>]+>|(?:请|麻烦|需要).{0,12}(?:提供|发送|报给|告诉).{0,12}"
                                r"(?:验证码|证件号|身份证|银行卡|密码)|(?:一定|肯定|确保|承诺).{0,8}(?:通过|办结)|"
                                r"(?:[一二两三四五六七八九十几]+|半)(?:个工作日|天内|小时内|分钟内)|"
                                r"(?:已为您|已经为您|已帮您)(?:提交|补传|通过|结案)|系统提示词|知识库占位符")
CLAIMED_OPERATION = re.compile(r"(?:已|已经|刚刚)(?:为您|帮您)?(?:登记|预约|转接|关单|办结)")
TERMINAL_ACTIONS = {"confirm", "callback", "handoff", "wrong_party", "end"}


def offer_script(case):
    return DIALOGUE_POLICY["templates"]["explain"].format(reason=case.reason, action=case.action)


def voice_templates(knowledge):
    # Prewarm common replies and the stable first-line call cases. Other scenarios are
    # synthesized on demand and share the bounded phrase cache during calls.
    common_cases = {"POP-001", "POP-002", "POP-003", "POP-005", "POP-008"}
    templates = knowledge.dialogue["templates"]
    return [
        knowledge.identity + knowledge.opening, templates["verify"], templates["acknowledged"],
        *filter(None, OPENINGS.values()),
        *(offer_script(case) for case in knowledge.cases.values() if case.id in common_cases and case.reason and case.action),
        *(entry.answer + templates["faq_end"] for entry in knowledge.entries.values() if not entry.source_rows),
        *(templates[key] for key in ("protected", "verified_retry", "gap", "handoff", "callback", "wrong_party")),
    ]


def redact(text):
    return SENSITIVE.sub("【已脱敏】", text)


class Dialogue:
    def __init__(self, knowledge, tools, store, case_id, mode, resolver=None, session_id=None, *,
                 answer_assistant=None, agent=None, followup_ticket=None):
        self.knowledge = knowledge
        self.tools = tools
        self.store = store
        self.case = knowledge.cases[case_id]
        self.resolver = resolver
        self.answer_assistant = answer_assistant
        self.agent = agent
        self.templates = knowledge.dialogue["templates"]
        self.history = deque(maxlen=6)
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
            text = self.templates["blocked"]
            source, fact = None, False
            answer_model = None
        response = {"text": text, "intent": intent, "source": source, "reason": reason, "fact": fact,
                    "state": self.state, "turn": self.index, "stages": self.stages[:],
                    "verified": self.verified, "receipt": self.receipt, "ticket": self.ticket, "written": self.written,
                    "disposition": self.disposition}
        if answer_model:
            response["answer_model"] = answer_model
            if self.agent:
                response["dialogue_agent"] = True
        self.store.log(self.id, "assistant", **response)
        if self.agent:
            for slot, fact in sorted(self.agent_facts().items(), key=lambda item: -len(item[1].text)):
                text = text.replace(fact.text, "{{" + slot + "}}")
            self.history.append(("assistant", text))
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
        return self.reply(answer + self.templates["faq_end"], f"faq:{self.last_entry}", source, fact=True,
                          answer_model=model)

    def start(self):
        if self.state != "idle":
            raise ValueError("会话已经开始")
        self.tool("lookup_list", self.tools.lookup_list, self.case.id)
        self.store.log(self.id, "contact_plan", channel=self.channel, submission_attempt=self.case.attempt,
                       contact_attempt=2 if self.followup_ticket else 1, followup_ticket=self.followup_ticket)
        if self.channel == "jingmai":
            return self.finish("jingmai", self.templates["jingmai"])
        self.mark("opening")
        self.state = "identity"
        return self.reply(self.knowledge.identity + self.knowledge.opening, "opening")

    def interrupt(self, response_intent):
        if response_intent == "opening":
            self.opening_interrupted = True
        if response_intent == "offer":
            self.offered = False

    def offer(self, text=None, source=None, answer_model=None):
        case = self.application
        if not case or not case.reason or not case.action:
            return self.finish("handoff", self.templates["case_missing"])
        text = text or offer_script(case)
        source = source or case.source
        if FORBIDDEN.search(text) or SENSITIVE.search(text):
            self.store.log(self.id, "compliance", action="blocked", severity="severe", reason="unsafe_case_content")
            return self.finish("handoff", self.templates["case_missing"])
        self.need_confirmed = True
        self.mark("needs")
        self.mark("offer")
        self.offered = True
        self.state = "confirm"
        self.last_factual_reply = (text, source)
        return self.reply(text, "offer", source, fact=True, answer_model=answer_model)

    def finish(self, disposition, text, *, answer_model=None):
        if self.state == "done":
            return self.reply(self.templates["already_done"], "done")
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
            return self.reply(self.templates["write_pending"], "write_pending", reason="tool")
        self.mark("writeback")
        self.state = "done"
        self.disposition = disposition
        return self.reply(text, disposition, answer_model=answer_model)

    def terminal(self, trigger):
        if self.state == "write_pending":
            return self.finish(self.pending_result["disposition"], self.templates["write_done"])
        if trigger == "no_answer" and not self.verified:
            if self.case.attempt == 2 and self.channel == "phone" and not self.followup_ticket:
                return self.finish("callback", self.templates["no_answer_callback"])
            return self.finish("jingmai", self.templates["no_answer_jingmai"])
        return self.finish("ended", self.templates["end"])

    def verify(self, text, answer_model=None):
        try:
            self.application = self.tool("lookup_application", self.tools.lookup_application, self.case.id, True)
        except (ValueError, KeyError, PermissionError, OSError, sqlite3.Error):
            return self.finish("handoff", self.templates["case_unavailable"])
        self.verified = True
        self.mark("identity")
        self.state = "needs"
        return self.reply(text, "verify", answer_model=answer_model)

    def verify_again(self, text, answer_model=None):
        self.identity_retries += 1
        if self.identity_retries >= 3:
            return self.finish("ended", self.templates["identity_exhausted"])
        return self.reply(text, "verify_again", answer_model=answer_model)

    def acknowledge(self, text, answer_model=None):
        if not self.offered:
            return self.offer()
        self.acknowledged = True
        self.mark("confirm")
        return self.finish("notified", text, answer_model=answer_model)

    def knowledge_gap(self, query, response=None, answer_model=None):
        self.unknown += 1
        self.store.log(self.id, "failure", reason="knowledge_gap", turn=self.index, query=query)
        if self.unknown >= 3:
            return self.finish("handoff", self.templates["gap_handoff"])
        return self.reply(response or self.templates["gap" if self.unknown == 1 else "gap_second"],
                          "clarify", reason="knowledge_gap", answer_model=answer_model)

    def agent_facts(self):
        if not self.verified or not self.application:
            return {}
        case = self.application
        facts = {entry.id: Fact(entry.question, entry.answer, entry.source)
                 for entry in self.knowledge.allowed_entries(case)}
        if case.reason and case.action:
            facts = {"case_reason": Fact("本单驳回原因", case.reason, case.source),
                     "case_action": Fact("本单整改步骤", case.action, case.source), **facts}
        return facts

    def agent_turn(self, text, key, input_mode):
        actions = {name: rule["guidance"] for name, rule in self.knowledge.dialogue["actions"].items()
                   if self.state in rule["states"]}
        if not actions:
            return None
        facts = self.agent_facts()
        start = time.monotonic()
        plan = self.agent.plan(text, stage=self.state, offered=self.offered, case=self.case,
                               history=self.history, actions=actions, facts=facts, input_mode=input_mode)
        self.history.append(("user", text))
        action = plan.action
        status = plan.status
        skeleton = SLOT.sub("", plan.text)
        if status == "used":
            if (FORBIDDEN.search(skeleton) or SENSITIVE.search(skeleton) or UNSUPPORTED_OUTPUT.search(skeleton)
                    or (not self.verified and BEFORE_IDENTITY.search(skeleton))
                    or (action not in TERMINAL_ACTIONS and CLAIMED_OPERATION.search(skeleton))):
                status = "unsafe_reply"
            elif action == "verify" and (IDENTITY_DOUBT.search(key) or not (
                    IDENTITY.fullmatch(key) or SELF_ATTESTATION.search(text))):
                status = "unconfirmed_identity"
            elif action == "confirm" and (FOLLOWUP_QUESTION.search(text) or NEGATIVE_CONFIRMATION.search(key)):
                status = "unanswered_question"
        self.store.log(self.id, "dialogue_agent", turn=self.index, stage=self.state, model=self.agent.model,
                       policy=self.knowledge.dialogue["version"],
                       status=status, action=action or None, references=list(plan.slots),
                       elapsed_ms=round((time.monotonic() - start) * 1000, 1))
        if status == "unconfirmed_identity":
            return self.verify_again(self.templates["verified_retry"])
        if status != "used":
            return None

        response = SLOT.sub(lambda match: facts[match[1]].text, plan.text)
        source = " ｜ ".join(dict.fromkeys(facts[slot].source for slot in plan.slots))
        model = self.agent.model
        if action == "verify":
            return self.verify(response, model)
        if action == "verify_again":
            return self.verify_again(response, model)
        if action == "explain":
            self.unknown = 0
            return self.offer(response, source, model)
        if action == "answer":
            self.unknown = 0
            self.last_entry = next((slot for slot in plan.slots if slot in self.knowledge.entries), None)
            self.last_factual_reply = (response, source)
            intent = f"faq:{self.last_entry}" if self.last_entry else "answer"
            return self.reply(response, intent, source, fact=True, answer_model=model)
        if action == "clarify":
            return self.knowledge_gap(text, response, model)
        if action == "confirm":
            return self.acknowledge(response, model)
        if action == "wrong_party":
            self.verified = False
        return self.finish("ended" if action == "end" else action, response, answer_model=model)

    def handle(self, raw, *, input_mode=None):
        text = self.knowledge.transcript(redact(raw))[:400]
        key = compact(text)
        self.index += 1
        self.store.log(self.id, "user", turn=self.index, text=text, state=self.state,
                       **({"input_mode": input_mode} if input_mode else {}))
        if self.state == "done":
            return self.reply(self.templates["already_done"], "done")
        if self.state == "write_pending":
            return self.terminal("retry")
        if PROBE.search(key):
            self.store.log(self.id, "compliance", action="refused", severity="protected",
                           reason="untrusted_input")
            return self.reply(self.templates["protected"], "protected")
        if self.state == "identity" and self.opening_interrupted:
            self.opening_interrupted = False
            return self.reply(self.knowledge.identity + self.knowledge.opening, "opening")
        if self.agent:
            response = self.agent_turn(text, key, input_mode)
            if response is not None:
                return response
        return self.handle_rules(text, key, input_mode)

    def handle_rules(self, text, key, input_mode):
        # Keep the local workflow available without inference, and for replayable regression tests.
        if WRONG.search(key):
            if self.verified:
                self.verified = False
                return self.finish("wrong_party", self.templates["wrong_party_verified"])
            return self.finish("wrong_party", self.templates["wrong_party"])
        if ESCALATE.search(key):
            return self.finish("handoff", self.templates["handoff"])
        if DEFER.search(key):
            return self.finish("callback", self.templates["callback"])
        if self.state == "identity":
            if IDENTITY.search(key):
                return self.verify(self.templates["verify"])
            return self.verify_again(self.templates["identity_origin" if IDENTITY_ORIGIN.search(key) else "verified_retry"])
        if self.state not in {"needs", "confirm"} or not self.verified:
            return self.reply(self.templates["idle"], "idle")
        if self.state == "needs":
            if ANGRY.search(key) or NEED.search(key):
                return self.offer()
            return self.reply(self.templates["needs_clarify"], "clarify_need")
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
            return self.reply(self.templates["empathy"], "empathy")
        question = bool(QUESTION.search(text))
        resolver = self.resolver if question and not self.answer_assistant else None
        entry = self.knowledge.retrieve(text, self.case, resolver)
        confirmed = not question and not NEGATIVE_CONFIRMATION.search(key) and bool(CONFIRM.search(key))
        if not confirmed and (entry or self.answer_assistant):
            answer = self.answer_question(text, entry, input_mode)
            if answer:
                return answer
        if confirmed:
            return self.acknowledge(self.templates["acknowledged"])
        return self.knowledge_gap(text)
