import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .faq_catalog import reviewed_faq

ROOT = Path(__file__).resolve().parents[1]
SCENE_PATH = ROOT / "data" / "scene.json"
DIALOGUE_PATH = ROOT / "data" / "dialogue_policy.json"
DIALOGUE_POLICY = json.loads(DIALOGUE_PATH.read_text(encoding="utf-8"))
REVISION = re.compile(r"[a-zA-Z0-9._-]{1,64}\Z")


def compact(text: str) -> str:
    return re.sub(r"[^\w\u3400-\u9fff]+", "", text.lower())


@dataclass(frozen=True)
class Case:
    id: str
    merchant: str
    brand: str
    name: str
    route: str
    scene_row: int
    reason: str
    action: str
    scope: list[str]
    attempt: int
    callback_requested: bool = False
    complaint: bool = False
    categories: list[str] = field(default_factory=list)
    source_cell: str = ""
    source_kind: str = "fixture"

    def channel(self) -> str:
        if self.callback_requested or self.complaint:
            return "phone"
        return "jingmai" if self.attempt >= 3 else self.route

    @property
    def source(self) -> str:
        source = f"资审关单场景话术V1.0.xlsx · 场景汇总!{self.scene_row}"
        return f"{source}（话术 {self.source_cell}）" if self.source_cell else source


@dataclass(frozen=True)
class Entry:
    id: str
    scopes: list[str]
    question: str
    aliases: list[str]
    answer: str
    source: str
    source_rows: list[int] = field(default_factory=list)


class Knowledge:
    def __init__(self, scene: dict, revision: str = "baseline", aliases: dict | None = None, *, training=()):
        self.title = scene["title"]
        self.version = scene["version"]
        self.revision = revision
        self.identity = scene["identity"]
        self.opening = scene["opening"]
        self.dialogue = DIALOGUE_POLICY
        self.domain_words = scene["domain_words"]
        self.training = tuple(training)
        self.cases = {item["id"]: Case(**item) for item in scene["cases"]}
        self.entries = {}
        for entry in (*scene["faq"], *self.training):
            extra = (aliases or {}).get(entry["id"], [])
            self.entries[entry["id"]] = Entry(**{**entry, "aliases": [*entry["aliases"], *extra]})

    @classmethod
    def load(cls, revision="baseline", aliases=None, *, catalog=None, training=()):
        scene = json.loads(SCENE_PATH.read_text(encoding="utf-8"))
        if catalog is not None:
            fixtures = {item["id"]: item for item in scene["cases"]}
            missing = {**fixtures["POP-009"], "id": "POP-TEST-MISSING", "name": "单据缺少可播报详情"}
            scene["cases"] = [
                *sorted(({**item, "source_kind": "reference"} for item in catalog), key=lambda item: item["id"]),
                fixtures["POP-006"], fixtures["POP-007"], missing,
            ]
            scene["faq"].extend(reviewed_faq())
        return cls(scene, revision, aliases, training=training)

    def transcript(self, text):
        for raw, clean in self.domain_words.items():
            text = text.replace(raw, clean)
        return text.strip()

    def allowed_entries(self, case: Case) -> list[Entry]:
        return [entry for entry in self.entries.values()
                if (not entry.source_rows or case.scene_row in entry.source_rows)
                and ("*" in entry.scopes or set(case.scope) & set(entry.scopes))]

    def retrieve(
        self,
        text: str,
        case: Case,
        resolve: Callable[[str, list[Entry]], str | None] | None = None,
    ) -> Entry | None:
        allowed = self.allowed_entries(case)
        query = compact(text)
        exact = [(len(alias), entry.id, entry) for entry in allowed
                 for alias in map(compact, entry.aliases) if alias and alias in query]
        if exact:
            return max(exact, key=lambda item: (item[0], item[1]))[2]
        grams = set(zip(query, query[1:]))
        scores = []
        for entry in allowed:
            reference = compact(entry.question)
            terms = set(zip(reference, reference[1:]))
            score = len(grams & terms)
            if score >= 3:
                scores.append((score / max(len(terms), len(grams)), entry))
        scores.sort(key=lambda item: item[0], reverse=True)
        if scores and scores[0][0] >= 0.40 and (len(scores) < 2 or scores[0][0] - scores[1][0] > 0.10):
            return scores[0][1]
        if resolve:
            selected = resolve(text, allowed)
            return next((e for e in allowed if e.id == selected), None)
        return None
