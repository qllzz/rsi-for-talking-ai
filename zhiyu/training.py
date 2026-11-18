from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from .dialog import FORBIDDEN, SENSITIVE
from .knowledge import compact


SCOPES = {"*": "通用", "authorization": "授权资质", "trademark": "商标", "shop": "店铺"}
STAGES = {"opening": "开场核验", "explain": "说明原因", "support": "咨询答复", "followup": "回电与复核", "closing": "确认与结束"}
SCRIPT_TEMPLATES = (
    ("verify", "opening", "核验身份后征求同意"), ("identity_origin", "opening", "回应来电来源顾虑"),
    ("explain", "explain", "说明驳回原因和整改步骤"), ("needs_clarify", "explain", "确认是否方便听取"),
    ("empathy", "support", "回应商家情绪"), ("gap_second", "support", "聚焦问题进一步澄清"),
    ("callback", "followup", "登记稍后回电"), ("handoff", "followup", "转人工复核"),
    ("acknowledged", "closing", "确认处理结果"), ("wrong_party", "closing", "接听人身份不符"),
)


class MaterialInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    question: str = Field(min_length=3, max_length=100)
    answer: str = Field(min_length=4, max_length=320)
    source: str = Field(min_length=2, max_length=100)
    scope: Literal["*", "authorization", "trademark", "shop"] = "*"
    aliases: list[str] = Field(default_factory=list, max_length=5)
    enabled: bool = True

    @field_validator("question", "source", "answer")
    @classmethod
    def reviewed_text(cls, value, info: ValidationInfo):
        if SENSITIVE.search(value) or (info.field_name == "answer" and FORBIDDEN.search(value)):
            raise ValueError("资料中包含敏感号码或不适合播报的内容")
        if info.field_name == "question" and len(compact(value)) < 3:
            raise ValueError("问题至少需要 3 个有效字符")
        return value

    @field_validator("aliases")
    @classmethod
    def reviewed_aliases(cls, values):
        aliases = list(dict.fromkeys(word.strip() for word in values if word.strip()))
        if any(not 2 <= len(compact(word)) <= 100 or SENSITIVE.search(word) for word in aliases):
            raise ValueError("每种问法应包含 2 至 100 个有效字符，且不含敏感号码")
        return aliases


class MaterialUpdate(MaterialInput):
    edit_version: int = Field(ge=1)


class MaterialConflict(ValueError):
    pass


def published_entry(material):
    return {"id": material["id"], "question": material["question"], "answer": material["answer"],
            "source": material["source"], "scopes": [material["scope"]],
            "aliases": list(dict.fromkeys([material["question"], *material["aliases"]]))}


def material_catalog(knowledge, drafts):
    active = {entry["id"]: entry for entry in knowledge.training}
    result = []
    for row in reversed(drafts):
        applied = active.get(row["id"])
        if row["enabled"]:
            status = "active" if applied == published_entry(row) else "pending"
        else:
            status = "pending_pause" if applied else "paused"
        result.append({**row, "status": status, "scopes": [row["scope"]], "source_rows": []})
    result.extend({"id": entry.id, "question": entry.question, "answer": entry.answer, "source": entry.source,
                   "aliases": entry.aliases, "scopes": entry.scopes, "source_rows": entry.source_rows, "status": "reviewed"}
                  for entry in knowledge.entries.values() if entry.id not in active)
    return result


def quick_scripts(knowledge):
    source = f"对话规则 · {knowledge.dialogue['version']}"
    templates = knowledge.dialogue["templates"]
    scripts = [{"id": "opening", "title": "开场自我介绍", "stage": "opening",
                "text": knowledge.identity + knowledge.opening, "source": "资审服务流程"}]
    for key, stage, title in SCRIPT_TEMPLATES:
        reply = templates[key].replace("{reason}", "【本单驳回原因】").replace("{action}", "【本单整改步骤】")
        scripts.append({"id": key, "title": title, "stage": stage, "text": reply, "source": source})
    scripts.extend({"id": f"faq-{entry.id}", "title": entry.question, "stage": "support",
                    "text": entry.answer, "source": entry.source} for entry in knowledge.entries.values())
    return scripts
