"""Load the reviewed POP V1.0 scene catalog from the local contest workbook."""

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path

from openpyxl import load_workbook


SHEET = "场景汇总"
ROUTES = {"电话": "phone", "京麦驳回": "jingmai"}
EXPECTED_COUNTS = {"电话": 54, "京麦驳回": 14}
# Hash the normalized A:E values so Office metadata and workbook formatting may change.
# The reviewed customer guidance below remains bound to the same rule and script contents.
REVIEWED_CONTENT = "e3d38cb3ff474d7b67dc2a490b868650ee5352a0cc758ea8f9339bb6e77ea8dc"
EXISTING_IDS = {15: "POP-004", 18: "POP-001", 19: "POP-002", 28: "POP-009",
                64: "POP-003", 71: "POP-005", 79: "POP-008"}
CASE_DETAILS = {"POP-001", "POP-002", "POP-003", "POP-005", "POP-008"}


@dataclass(frozen=True)
class SourceRule:
    row: int
    categories: tuple[str, str, str]
    route: str
    script_cell: str


def find_source(folder: Path) -> Path | None:
    candidates = sorted(path for path in folder.glob("*资审*V1.0.xlsx")
                        if path.is_file() and not path.name.startswith("~$"))
    if len(candidates) > 1:
        raise ValueError("发现多个资审 V1.0 工作簿，请明确指定需要使用的原表。")
    return candidates[0] if candidates else None


def merged_cell(sheet, row, col):
    cell = sheet.cell(row, col)
    if cell.value is None:
        for area in sheet.merged_cells.ranges:
            if area.min_row <= row <= area.max_row and area.min_col <= col <= area.max_col:
                return sheet.cell(area.min_row, area.min_col)
    return cell


def cell_text(cell):
    return " ".join(str(cell.value or "").split())


def read_rules(workbook: Path) -> list[SourceRule]:
    rules = []
    normalized = []
    # Workbooks are read only. Supplying the stream also allows Excel to keep the file open.
    with workbook.open("rb") as file:
        book = load_workbook(file, data_only=True)
        try:
            if SHEET not in book.sheetnames:
                raise ValueError("原表缺少「场景汇总」工作表。")
            sheet = book[SHEET]
            for row in range(1, sheet.max_row + 1):
                route = cell_text(merged_cell(sheet, row, 4))
                if route not in ROUTES:
                    continue
                categories = tuple(cell_text(merged_cell(sheet, row, col)) for col in (1, 2, 3))
                script = merged_cell(sheet, row, 5)
                script_text = cell_text(script)
                if not all(categories) or not script_text:
                    raise ValueError(f"「场景汇总」第 {row} 行缺少分类或话术依据。")
                rules.append(SourceRule(row, categories, route, script.coordinate))
                normalized.append([row, *categories, route, script_text])
        finally:
            book.close()
    counts = Counter(rule.route for rule in rules)
    if counts != EXPECTED_COUNTS or {rule.row for rule in rules} != set(GUIDANCE):
        raise ValueError("「场景汇总」的行号或渠道数量不符合已验收的 68 条 V1.0 规则。")
    content = json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if sha256(content).hexdigest() != REVIEWED_CONTENT:
        raise ValueError("「场景汇总」的分类或话术内容已更新，请核对规则及对客说明后再启用。")
    return rules


def scopes_for(rule: SourceRule) -> list[str]:
    _, category, detail = rule.categories
    scopes = []
    if "授权" in category or "授权书" in detail:
        scopes.append("authorization")
    if "商标" in category or "持有人" in detail:
        scopes.append("trademark")
    if "店铺名称" in category or "店铺名称" in detail:
        scopes.append("shop")
    return scopes


def load_catalog(workbook: Path, *, fixtures: Iterable[dict] = ()) -> list[dict]:
    rules = read_rules(workbook)
    fixtures = {case["id"]: case for case in fixtures}
    cases = []
    next_id = 10
    for rule in rules:
        case_id = EXISTING_IDS.get(rule.row)
        if not case_id:
            case_id = f"POP-{next_id:03d}"
            next_id += 1
        fixture = fixtures.get(case_id, {})
        reason, action = GUIDANCE[rule.row]
        if case_id in CASE_DETAILS:
            reason = fixture.get("reason") or reason
            action = fixture.get("action") or action
        cases.append({
            "id": case_id,
            "merchant": fixture.get("merchant", "资质审核单据"),
            "brand": fixture.get("brand", ""),
            "name": fixture.get("name", rule.categories[-1]),
            "route": ROUTES[rule.route],
            "scene_row": rule.row,
            "categories": list(rule.categories),
            "source_cell": rule.script_cell,
            "reason": reason,
            "action": action,
            "scope": scopes_for(rule),
            "attempt": fixture.get("attempt", 1),
        })
    return sorted(cases, key=lambda case: case["id"])


# Reviewed speaking points. Each pair is a reason and an action in the syntax required
# by the call template. When the sheet uses a blank or alternatives for specific details,
# direct the merchant to their authenticated application instead of inventing a value.
GUIDANCE = {
    13: ("商标的当前状态不满足本次申请要求", "核对驳回单据标明的状态和类别要求，补充符合要求的商标资料"),
    14: ("商标的注册时间或有效期不满足本次申请要求", "核对驳回单据注明的时间要求，按书面意见处理后重新提交"),
    15: ("提交的证明材料存在需要进一步核实的修改痕迹", "查看驳回单据并提交真实有效的对应证明材料"),
    16: ("申请目前受平台管控，暂不支持办理", "在京麦查看本单的管控说明，需要核实时通过京麦客服咨询"),
    17: ("本次提交的授权书内容不完整", "按驳回单据列出的缺项补齐授权书后重新提交"),
    18: ("提交的品牌资质不齐全", "按京麦原单据列出的资质缺项补充材料后重新提交"),
    19: ("申报的品牌持有人与商标公示信息不一致", "核对商标公示信息，修正申请和对应材料后重新提交"),
    20: ("新增品牌与店铺现有经营品牌的持有人不一致", "在京麦品牌管理中核对现有品牌。如果原单要求停用，先确认业务影响，再停用对应品牌并重新申请新增"),
    21: ("提交的品牌授权书已经失效", "联系授权方重新开具有效授权书，替换原申请材料后重新提交"),
    22: ("提交的品牌授权链缺少必要的授权关系", "按驳回单据列出的缺失环节补全授权链路后重新提交"),
    23: ("申请中的店铺名称与授权书所列店铺名称不一致", "核对两处店铺名称，修正名称或补交匹配的授权书后重新提交"),
    24: ("当前申请类目不支持使用无品牌资质", "核对该类目的品牌要求和驳回单据说明，再决定是否按要求申请"),
    25: ("用于证明授权的材料存在需要核实的修改痕迹", "按驳回单据要求提供真实有效的授权证明材料"),
    26: ("本次授权核验未收到授权方回复", "与授权方确认核验回复情况，按驳回单据要求在资料核实后重新提交"),
    27: ("品牌方反馈与提交的授权信息不一致", "先与品牌方核对真实有效的授权，再按驳回意见重新提交"),
    28: ("品牌方未认可本次提交的授权", "先与品牌方核实授权内容，待信息核对无误后重新提交"),
    29: ("申请类目与现有授权限制不匹配", "核对授权书中的经营范围和申请中的类目设置，按照驳回意见更正后提交"),
    30: ("申请中存在超出授权适用范围的类目", "更新能覆盖申请类目的授权材料，或从申请中去除超范围类目后重新提交"),
    31: ("申请受当前品牌招商或平台规则限制", "查看京麦书面说明，并在需要时通过京麦核实当前品牌适用的申请条件"),
    32: ("本次申请不符合该品牌或类目的准入条件", "按驳回单据核对具体的招商或管控限制，通过京麦核实能否继续申请"),
    33: ("未核验到与申请产品类目相匹配的商标", "核对品牌和申请类目的对应关系，选用匹配的品牌或类目后重新提交"),
    34: ("新增产品线所用的商标当前无效", "确认可使用的有效品牌资质，按产品线申请要求重新提交"),
    35: ("本次选择的品牌与新增产品线所申请的类目不一致", "核对品牌与类目后更正申请，再重新提交"),
    36: ("当前产品线申请不支持所提交的图形商标", "按驳回单据要求更换符合准入条件的品牌后重新提交"),
    37: ("申请品牌存在容易引起混淆的问题，当前不符合产品线准入要求", "在京麦查看书面审核意见，核实可用于申请的品牌范围"),
    38: ("申请类目不在商标可使用的类似群范围内", "按驳回单据核对允许的类似群，选择符合范围的品牌或类目后重新提交"),
    39: ("当前所属事业部不支持商家自行提交该产品线申请", "有新增产品线需求时，联系您的采销对接人核实办理方式"),
    40: ("该全球购申请需要境外有效注册证，现有材料为境内注册证", "按单据要求补充适用于申请的境外有效注册证后重新提交"),
    41: ("该品牌当前不符合相应全球购招商要求", "核对准入要求，按驳回单据更换符合条件的品牌后重新提交"),
    42: ("上传的全球购品牌授权材料与申请条件不匹配", "按店铺类型及驳回说明，核对品牌、权利人、授权链路和独占性，并提交符合要求的授权资料"),
    43: ("医药类目申请缺少符合要求的购货证明或完整授权链", "按单据要求通过正式申请入口上传有效的购货发票或完整授权链路，再重新提交"),
    44: ("全球购申请的对应资质材料不齐全", "核对京麦单据列出的具体缺项，补齐后重新提交"),
    45: ("提交的全球购证明材料存在需要核实的修改痕迹", "查看驳回单据，提供真实有效的材料供重新审核"),
    46: ("企业公开登记状态未满足本次入驻要求", "在国家企业信用信息公示系统核对状态，按书面意见联系当地市场监管部门处理，状态符合要求后重新提交"),
    47: ("本次上传的企业主体证明材料已经到期", "按驳回单据核对到期材料，更新为有效版本后重新提交"),
    48: ("申请中的主体名称或营业执照登记信息填写有误", "核对营业执照与申请字段，按登记内容更正后重新提交"),
    49: ("申请中选择的法人证件类型与上传资料的类型不一致", "在申请入口更正证件类型并上传匹配的资料，再重新提交"),
    50: ("入驻申请的授权书内容不完整", "在原申请单中核对授权书缺项，补齐后重新提交"),
    51: ("入驻申请所用的授权书已经失效", "向授权方索取新的有效授权书，更新入驻单据后重新提交"),
    52: ("入驻申请所提交的授权链路不完整", "在驳回单据中核对授权关系的缺项，补齐对应授权材料后重新提交"),
    53: ("入驻申请缺少所需的品牌相关资质", "按驳回单据补齐列明的品牌资质后重新提交"),
    54: ("入驻申请中的品牌持有人与核验结果不一致", "核对实际权利人和相关证明，修正品牌持有人信息后重新提交"),
    55: ("所选店铺类型不支持以无品牌方式入驻", "核对店铺类型的品牌准入要求，按京麦书面说明调整申请"),
    56: ("本次入驻审核未能联系到授权方确认授权", "联系授权方协助核实，并通过原单据补充核验所需材料，待信息确认后重新提交"),
    57: ("目前还未能核实入驻申请的授权信息", "与授权方核对，并查看京麦原单据是否需要补交相应的核验材料"),
    58: ("入驻申请的授权信息与品牌方反馈不一致", "先与品牌方确认授权内容，信息核对无误后再按书面意见提交"),
    59: ("品牌方未认可入驻申请所使用的授权", "与品牌方核实授权内容和授权范围，确认无误后重新提交入驻单据"),
    60: ("入驻申请中的经营限制或品牌类目设置与授权不符", "按驳回单据核对品牌经营范围并修正类目或权限设置，然后重新提交"),
    61: ("入驻申请所填类目超出现有授权适用范围", "补充可覆盖相应类目的授权信息，或去掉超范围类目后重新提交"),
    62: ("入驻申请中提交的商标状态不满足审核要求", "按入驻单据说明提供符合状态与类别要求的商标资料后重新提交"),
    63: ("入驻申请中的商标注册时间或有效期不符合条件", "按驳回单据核对商标时间要求，处理后重新提交"),
    64: ("入驻申请中的店铺名称不符合命名规范", "按京麦单据提示修正店铺名称后重新提交"),
    65: ("入驻申请的店铺名称与对应授权书中的名称不一致", "修改店铺名称或更新为与申请名称一致的授权书，然后重新提交"),
    66: ("当前申请不符合相应的平台准入规则", "在京麦查看具体的招商或管控说明，有疑问时通过京麦客服核实"),
    67: ("入驻单据上对应的行业资质缺失或上传有误", "按驳回单据在指定栏目补充或替换对应的行业资质后重新提交"),
    68: ("入驻申请的经营类目与行业资质范围不匹配", "核对所选类目和证照范围，更新匹配的资质或按要求更正类目后重新提交"),
    69: ("入驻申请所用的行业资质已超过有效期", "按入驻单据要求上传有效的行业资质材料，再重新提交"),
    70: ("入驻申请提供的行业资质目前未通过真实性核验", "按驳回单据说明核对资料并提供真实有效、可核验的证明材料"),
    71: ("行业资质材料缺失或上传到了不对应的栏目", "在驳回单据注明的资质栏目补充正确的材料后重新提交"),
    72: ("提交的行业资质范围不能覆盖所申请的经营类目", "核对证照范围和所选类目，按单据要求补交匹配材料后重新提交"),
    73: ("提交的行业资质已经超过有效期", "在对应资质栏替换为有效的证明材料后重新提交"),
    74: ("提交的行业资质目前未通过真实性核验", "按京麦驳回意见核对资质并补充真实有效且可核验的证明"),
    75: ("公司资质单据中的法人证件信息填写有误", "通过申请页面核对并更正证件信息，再重新提交"),
    76: ("公司资质申请选择的法人证件类型与上传文件不一致", "核对驳回单据中的证件类型要求，通过申请页面上传匹配的文件后重新提交"),
    77: ("所填税务识别信息与登记的商家资料不一致", "在原申请页面核对并更正税务识别信息后重新提交"),
    78: ("申请录入的营业执照信息与上传的证照图片不一致", "核对证照登记内容和上传图片，更正不一致的资料后重新提交"),
    79: ("本次商品申请提供的授权书内容不完整", "核对驳回单据列明的缺项，补齐对应授权材料后重新提交"),
    80: ("当前商品单据缺少相符的资质或上传了不对应的文件", "在商品原单的指定栏目补传或替换正确的资质后重新提交"),
}
