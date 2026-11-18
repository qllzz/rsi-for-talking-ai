from collections import Counter
import json
import os
from pathlib import Path

from openpyxl import load_workbook
import pytest

from zhiyu.catalog import SHEET, find_source, load_catalog
from zhiyu.dialog import FORBIDDEN, SENSITIVE, offer_script
from zhiyu.knowledge import Knowledge, ROOT, SCENE_PATH


@pytest.fixture(scope="module")
def source_workbook():
    configured = os.environ.get("ZHIYU_RULES_FILE")
    source = Path(configured) if configured else find_source(ROOT / "reference") or find_source(ROOT.parent)
    if source is None:
        pytest.skip("本地没有比赛原表")
    return source


def test_find_source_ignores_office_lock_and_rejects_ambiguous_versions(tmp_path):
    source = tmp_path / "POP资审V1.0.xlsx"
    source.touch()
    (tmp_path / ("~$" + source.name)).touch()
    assert find_source(tmp_path) == source
    (tmp_path / "另一份资审V1.0.xlsx").touch()
    with pytest.raises(ValueError, match="多个"):
        find_source(tmp_path)


def test_catalog_merges_preserve_source_and_all_offers_are_safe(source_workbook):
    fixtures = json.loads(SCENE_PATH.read_text(encoding="utf-8"))["cases"]
    raw_cases = load_catalog(source_workbook, fixtures=fixtures)
    knowledge = Knowledge.load(catalog=raw_cases)
    cases = {case.scene_row: case for case in knowledge.cases.values() if case.source_kind == "reference"}
    assert len(raw_cases) == len({case["id"] for case in raw_cases}) == 68
    assert set(cases) == set(range(13, 81))
    assert Counter(case.channel() for case in cases.values()) == {"phone": 54, "jingmai": 14}
    assert cases[28].source_cell == "E27" and cases[59].source_cell == "E58"
    assert cases[28].id == "POP-009" and cases[28].reason and cases[28].action
    assert cases[18].id == "POP-001" and cases[18].merchant == next(
        fixture["merchant"] for fixture in fixtures if fixture["id"] == "POP-001"
    )
    for case in cases.values():
        assert len(case.categories) == 3
        assert case.reason and case.action
        speech = offer_script(case)
        assert "【" not in speech and "XXX" not in speech
        assert not FORBIDDEN.search(speech) and not SENSITIVE.search(speech)


def test_changed_source_requires_review(source_workbook, tmp_path):
    changed = tmp_path / source_workbook.name
    with source_workbook.open("rb") as stream:
        book = load_workbook(stream, data_only=True)
        try:
            book[SHEET]["C13"] = "未复核的规则修订"
            book.save(changed)
        finally:
            book.close()
    with pytest.raises(ValueError, match="内容已更新"):
        load_catalog(changed)


def test_reviewed_faq_applies_only_to_the_relevant_source_row(source_workbook):
    knowledge = Knowledge.load(catalog=load_catalog(source_workbook))
    cases = {case.scene_row: case for case in knowledge.cases.values() if case.source_kind == "reference"}
    question = "发票需要盖章吗？"
    entry = knowledge.retrieve(question, cases[43])
    assert entry and entry.id == "invoice_stamp" and "高频QA!8" in entry.source
    assert not knowledge.retrieve(question, cases[64])
    assert not SENSITIVE.search(entry.answer)
