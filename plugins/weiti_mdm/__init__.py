"""IPN 自动编码插件（微体科技物料编码）

编码结构: {小类码}-{规格段}
  例: 201-030110301

规则说明（编码规则文档仅作格式参考，全部码值由系统自动分配，
           日常操作不需要查文档）:
  1. 类别码: 大类(顶层)手动定 1~8；小类创建时自动取「该号段下一个可用号」
     并自动改写名称前缀，如 在「2-结构类」下新建「螺丝」→ 自动变「201-螺丝」
  2. 选项码: 参数模板的选项值自动编号——只写 "金属膜/碳膜"，
     保存后自动变 "01-金属膜/02-碳膜"（已带码的保留并归一化位宽）
  3. 规格段: 按「类别参数模板的排列顺序」取每个参数值的前缀数字拼接
     - 参数值 "03-金属膜电阻" → 取 "03"
     - 参数值 "10K"/"4.7K" 等阻值 → 按 有效数字+10的幂 编码(10K→103, 4.7K→472)
     - 无数字前缀且非阻值 → 兜底 "00"
     - 参数没填全 → 发 !201-03??103? 式槽位码（?按字段位宽填=待补槽位）；
       无论自动还是手工创建，只要含未设置的参数就以 ! 开头（待完善/复核），
       参数补齐后自动升级为正式码、! 消失
     - 类别没绑参数模板 → 发 201-S0001 式无规格流水码（S=Spec-less，
       正式码不是临时态，无"待补"概念）
  4. 导入兼容: Excel 旧类别名经 keywords 字段自动归类(alias.txt 别名表 +
     类别名匹配)；中文单位(个/片/只)自动换算为系统单位(pcs)
  5. 描述反解参数: 零件保存后(含导入)自动从 description 按类别参数模板匹配参数值,
     模板有选项就找选项文本(01-SMD0603/SMD0603/0603 都认),阻值类抽 10K 等;
     写入参数后自动触发 IPN 从临时码升级为正式码。开关: ENABLE_DESC_EXTRACT

部署: 本目录放入 InvenTree 数据卷 plugins/ 下，重启容器后到
     管理员中心 → 插件 启用「IPN自动编码」。
"""

import logging
import os
import re

from django.core.exceptions import ValidationError
from django.db.models.signals import post_save, pre_save

from plugin import InvenTreePlugin
from plugin.mixins import (
    EventMixin, ScheduleMixin, SettingsMixin,
    UrlsMixin, UserInterfaceMixin, ValidationMixin)

logger = logging.getLogger('inventree')

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
ALIAS_FILE = os.path.join(PLUGIN_DIR, 'alias.txt')

# 插件目录是按文件路径加载的，不在 sys.path 中——手动挂上，
# 使 `import bom_import` 可用；并把本模块登记为 'weiti_mdm'，
# 供 bom_import.py 内 `from weiti_mdm import xxx` 解析。
import sys as _sys
_sys.modules.setdefault('weiti_mdm', _sys.modules[__name__])
if PLUGIN_DIR not in _sys.path:
    _sys.path.append(PLUGIN_DIR)

# ------------------------------------------------------------------
# 常量与正则
# ------------------------------------------------------------------

# 小类名前缀: "102-电阻" -> 102
CATEGORY_CODE_RE = re.compile(r'^(\d{3})[-\s_]+')
# 大类名前缀: "2-结构类" -> 2
TOP_CODE_RE = re.compile(r'^(\d)[-\s_]+')
# 参数选项值前缀: "03-金属膜电阻" -> 03
OPT_CODE_RE = re.compile(r'^(\d+)')
# IPN 成品格式: {小类码}-{规格段}，规格段=参数码按序拼接(无流水号)，
# 如 102-030110301；无规格件: 805-S0001(S=Spec-less 流水码)；
# 槽位码(参数不全): 一律带 ! 前缀，如 !102-03??103?（?按字段位宽）
IPN_RE = re.compile(r'^\d{3}-[0-9A-Za-z?]{2,}$')
IPN_PROV_RE = re.compile(r'^!\d{3}-[0-9A-Za-z?]+$')

SERIAL_LEN = 4
FALLBACK_CODE = '00'  # 参数值无数字前缀时的兜底段


# ------------------------------------------------------------------
# 类别自动编号
# ------------------------------------------------------------------

def auto_code_category(instance):
    """新建小类时自动分配码段并改写名称前缀。

    - 名称已带 "NNN-" 前缀 → 不动（用户手动指定了码）
    - 顶层大类（无 parent）→ 不动（1~8 手动定）
    - 父类名首位数字为大类号，取该号段内下一个可用码
    """
    from part.models import PartCategory

    name = (instance.name or '').strip()
    if not name or CATEGORY_CODE_RE.match(name):
        return
    if not instance.parent:
        return

    m = TOP_CODE_RE.match(instance.parent.name or '')
    if not m:
        logger.warning('WeiTiMDM: 父类别 "%s" 名称无大类码前缀，跳过自动编号',
                       instance.parent.name)
        return

    prefix = m.group(1)
    # 全库查该号段已用码（不只是兄弟）——三级类如「结构/紧固/定位」
    # 父类首码也是 2，若只查兄弟会和二级的 203 撞码
    used = set()
    for c in PartCategory.objects.all():
        mm = CATEGORY_CODE_RE.match(c.name or '')
        if mm and mm.group(1).startswith(prefix):
            used.add(int(mm.group(1)[1:]))

    for n in range(1, 100):
        if n not in used:
            new_name = f'{prefix}{n:02d}-{name}'
            logger.info('WeiTiMDM: 类别自动编号 "%s" -> "%s"', name, new_name)
            instance.name = new_name
            return

    logger.error('WeiTiMDM: 大类 %s 码段已用尽，无法为 "%s" 编号', prefix, name)


def check_category_code_unique(instance):
    """手输带码类别名的撞码检查：三位码全库唯一，撞码抛 ValidationError 阻断保存。

    自动编号路径不会撞（它扫过已用码），这里只拦"用户手动输入了已占用码"，
    如已有 101-传感器 又手建 101-PCB。
    """
    name = (instance.name or '').strip()
    m = CATEGORY_CODE_RE.match(name)
    if not m:
        return
    code = m.group(1)
    from part.models import PartCategory

    # 码段归属：首位必须等于上级大类的首码（3-线材类 下只能是 3xx）
    parent = getattr(instance, 'parent', None)
    if parent is not None:
        pm = TOP_CODE_RE.match(parent.name or '')
        if pm and code[0] != pm.group(1):
            raise ValidationError(
                f'类别码 {code} 不属于「{parent.name}」的号段，'
                f'该大类下只能用 {pm.group(1)}xx')

    for c in PartCategory.objects.exclude(pk=instance.pk).only('name'):
        mm = CATEGORY_CODE_RE.match(c.name or '')
        if mm and mm.group(1) == code:
            raise ValidationError(
                f'类别码 {code} 已被「{c.name}」占用，请更换编号')


def auto_code_template_options(instance):
    """参数模板选项自动编号：choices 里逗号/分号/换行分隔的选项，
    已带 "NN-" 前缀的保留原码，未带的取下一个可用号。

    输入 "金属膜,碳膜" → 保存后变 "01-金属膜,02-碳膜"
    """
    raw = (instance.choices or '').strip()
    if not raw:
        return

    lines = [ln.strip() for ln in re.split(r'[\n;,，；]', raw) if ln.strip()]
    if len(lines) == 1 and len(re.findall(r'\d{2}-', lines[0])) > 1:
        # 兼容旧bug数据：选项曾被无缝拼接 "01-A02-B" → 按编码边界重切
        lines = [ln.strip() for ln in re.split(r'(?=\d{2}-)', lines[0])
                 if ln.strip()]
    if not lines:
        return

    # 已带码的选项先登记占用（"01-金属膜"、"1-金属膜" 都算带码）
    used = set()
    for ln in lines:
        m = OPT_CODE_RE.match(ln)
        if m and ln[m.end():m.end() + 1] in ('-', ' '):
            used.add(int(m.group(1)))

    out = []
    for ln in lines:
        m = OPT_CODE_RE.match(ln)
        if m and ln[m.end():m.end() + 1] in ('-', ' '):
            # 已带码 → 保留原始位数: "0-±1%" 仍是1位码, "03-金属膜" 仍是2位码
            # (规格段位宽由编码表决定: 精度/功率1位, 类别/封装2位, 阻值3位)
            rest = ln[m.end():].lstrip('- ').strip()
            out.append(f'{m.group(1)}-{rest}')
            continue
        n = 1
        while n in used:
            n += 1
        used.add(n)
        out.append(f'{n:02d}-{ln}')

    new_choices = ','.join(out)
    if new_choices != raw:
        logger.info('WeiTiMDM: 模板 %s 选项自动编号 -> %s',
                    instance.name, new_choices)
        instance.choices = new_choices


# ------------------------------------------------------------------
# 导入兼容：旧类别名 / 中文单位
# ------------------------------------------------------------------

# 中文单位 -> 系统物理单位（可按需加）
UNIT_ALIAS = {
    '个': 'pcs', '片': 'pcs', '只': 'pcs', '件': 'pcs', '套': 'pcs',
    '颗': 'pcs', '条': 'pcs', '根': 'pcs', '块': 'pcs', '张': 'pcs',
    '米': 'm', '毫米': 'mm', '克': 'g', '千克': 'kg', '公斤': 'kg',
}

_ALIAS_CACHE = None


def load_category_alias():
    """别名表: 插件目录下 alias.txt，每行 "旧名=类别码"，如 紧固/定位=201。"""
    global _ALIAS_CACHE
    if _ALIAS_CACHE is not None:
        return _ALIAS_CACHE
    _ALIAS_CACHE = {}
    try:
        with open(ALIAS_FILE, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                k, v = line.split('=', 1)
                _ALIAS_CACHE[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    except Exception:
        logger.exception('WeiTiMDM: alias.txt 读取失败')
    return _ALIAS_CACHE


def _cat_by_code(code):
    from part.models import PartCategory
    for c in PartCategory.objects.all():
        m = CATEGORY_CODE_RE.match(c.name or '')
        if m and m.group(1) == str(code):
            return c
    return None


def match_category_from_text(text):
    """从一段文本（导入时塞进 keywords 的旧类别名）匹配类别。

    匹配顺序：别名表 > 完整名 > 去码名。候选按名称长度倒序，防短名误伤。
    """
    from part.models import PartCategory
    if not text:
        return None
    text = str(text).strip().lower()
    if not text:
        return None

    # 1) 别名表（旧Excel分类名 -> 类别码）
    for raw, code in load_category_alias().items():
        if raw.lower() in text:
            cat = _cat_by_code(code)
            if cat:
                return cat

    # 2) 类别名匹配：先比全名(201-螺丝螺母)，再比去码名(螺丝螺母)
    cands = []
    for c in PartCategory.objects.all():
        name = (c.name or '').strip()
        if not name:
            continue
        bare = CATEGORY_CODE_RE.sub('', name).strip()
        for cand in {name, bare}:
            if cand and len(cand) >= 2 and cand.lower() in text:
                cands.append((len(cand), c))
    if cands:
        cands.sort(key=lambda x: -x[0])
        return cands[0][1]
    return None


def fix_import_fields(part):
    """导入兼容处理：
    - category 为空且 keywords 含旧类别名 → 自动归类
    - 中文单位 → 换算成系统单位
    """
    if not part.category and getattr(part, 'keywords', None):
        cat = match_category_from_text(part.keywords)
        if cat:
            logger.info('WeiTiMDM: 按 keywords "%s" 自动归类 -> %s',
                        part.keywords, cat.name)
            part.category = cat

    units = (getattr(part, 'units', None) or '').strip()
    if units and units in UNIT_ALIAS:
        part.units = UNIT_ALIAS[units]


# ------------------------------------------------------------------
# IPN 特征段编码
# ------------------------------------------------------------------

def encode_resistance(text):
    """阻值编码: D1D2=两位有效数字, D3=10的幂，恒定 3 位。
    10K -> 103, 4.7K -> 472, 100Ω -> 101, 1M -> 105, 180V -> 181
    无法解析或超范围返回 None（交给上层兜底/占位）。
    """
    m = re.match(r'([\d.]+)\s*([kKmM]?)(?:\s*(?:Ω|ohm|R|欧))?', str(text).strip())
    if not m:
        return None
    try:
        val = float(m.group(1))
    except ValueError:
        return None
    ohms = val * {'': 1.0, 'k': 1e3, 'm': 1e6}[m.group(2).lower()]
    if ohms <= 0:
        return None
    exp = 0
    while ohms >= 100:
        ohms /= 10
        exp += 1
    while ohms < 10:
        ohms *= 10
        exp -= 1
    sig = int(round(ohms))          # 归一化后有效数字应落在 [10, 99]
    if sig >= 100:                  # 边界: 如 99.6 四舍五入到 100
        sig //= 10
        exp += 1
    if exp < 0 or exp > 9:
        return None  # 超出单位数幂范围，交给兜底
    return f'{sig:02d}{exp}'        # 两位有效数字 + 一位幂 = 恒 3 位


def param_code(value, template=None):
    """参数值 -> 编码段。

    传入 template 时按字段类型精确编码，并对齐到该字段位宽:
      - 有选项  : 取所选项前缀数字码，左补零到位宽（"3-金属膜"→"03"）
      - 数值型  : 走 encode_resistance，恒 3 位；无法解析 -> '?'*位宽
      - 其它    : 取开头连续数字（流水号/型号/版本原样），左补零到位宽
    不传 template 时（BOM 文本匹配等）用启发式:
      "NN-文字" 视为选项码取前缀；带单位的阻值(10K)走 encode_resistance。
    """
    s = str(value).strip()

    if template is not None:
        w = _slot_width(template)
        if _template_choices(template):
            m = re.match(r'^(\d+)', s)
            return m.group(1).zfill(w) if m else '?' * w
        if _is_numeric_template(template):
            r = encode_resistance(s)
            return r if r else '?' * w
        m = re.match(r'^(\d+)', s)
        return m.group(1).zfill(w) if m else '?' * w

    # 无 template：启发式（选项码带横杠，阻值带单位/无横杠）
    m = re.match(r'^(\d+)\s*-', s)
    if m:
        return m.group(1)
    r = encode_resistance(s)
    if r:
        return r
    m = OPT_CODE_RE.match(s)
    return m.group(1) if m else FALLBACK_CODE


def get_category_code(part):
    """零件类别名前缀取码: "102-电阻" -> "102"。"""
    cat = getattr(part, 'category', None)
    if not cat:
        return None
    m = CATEGORY_CODE_RE.match(cat.name or '')
    return m.group(1) if m else None


def _part_params(part):
    """取零件的 {参数模板名: 值} 字典。新旧参数API自适应。"""
    params = {}
    # 新旧版 part.parameters 都是 GenericRelation/RelatedManager
    try:
        for p in part.parameters.all():
            params[p.template.name] = p.data
        return params
    except Exception:
        pass
    if part.pk is None:
        return params
    # 兜底：泛型参数模型直查
    try:
        from django.contrib.contenttypes.models import ContentType
        from common.models import Parameter
        ct = ContentType.objects.get_for_model(part)
        for p in Parameter.objects.filter(model_type=ct, model_id=part.pk):
            params[p.template.name] = p.data
    except Exception:
        try:
            from part.models import PartParameter
            for p in PartParameter.objects.filter(part=part):
                params[p.template.name] = p.data
        except Exception:
            pass
    return params


def _is_numeric_template(template):
    """模板是否为"数值型"（阻值/容值/感值/压值），需走 encode_resistance 编码。"""
    name = (getattr(template, 'name', '') or '').lower()
    return any(k in name for k in ('阻值', '电阻值', 'resistance', '欧姆',
                                   '容值', '感值', '压值'))


def _slot_width(template):
    """单个参数模板在规格段中占的位宽（未填时占位符按此宽度出 ?）。

    - 有选项: 选项码的最大位数（"0-±1%"→1位, "03-金属膜"→2位）
    - 无选项且像数值型（阻值/容值/感值/压值）: 3位（D1D2有效数字+D3幂）
    - 其他: 2位兜底
    """
    widths = []
    for c in _template_choices(template):
        m = OPT_CODE_RE.match(str(c).strip())
        widths.append(len(m.group(1)) if m else len(FALLBACK_CODE))
    if widths:
        return max(widths)
    if _is_numeric_template(template):
        return 3
    return len(FALLBACK_CODE)


def feature_slots(part):
    """规格槽位串：按类别绑定顺序，已填参数出码、未填槽位按位宽用 ? 占位。

    返回 None = 类别无模板绑定且零件无参数（无法构造槽位）。
    含 ? = 规格未录全（临时码用）；不含 ? = 规格完整（正式特征段）。
    空槽位占位宽度 = 该参数的字段位宽（阻值 3 位→???，精度 1 位→?）。
    """
    cat = getattr(part, 'category', None)
    if not cat:
        return None

    tpls = category_templates(cat)
    params = _part_params(part)

    if not tpls:
        # 类别没绑模板：只有已填参数可拼（无槽位概念）
        names = sorted(params.keys())
        if not names:
            return None
        return ''.join(param_code(params[n]) for n in names)

    segs = []
    for tpl in tpls:
        val = params.get(tpl.name)
        if val is None or str(val).strip() == '':
            segs.append('?' * _slot_width(tpl))
        else:
            segs.append(param_code(val, tpl))
    return ''.join(segs)


def feature_segment(part):
    """完整规格段（参数全齐才有）：供正式码和 BOM 去重使用。"""
    seg = feature_slots(part)
    if not seg or '?' in seg:
        return None
    return seg


def next_serial(prefix):
    """同前缀下最大流水号 +1。"""
    from part.models import Part
    nums = []
    for ipn in (Part.objects.filter(IPN__startswith=prefix)
                .values_list('IPN', flat=True)):
        tail = str(ipn).rsplit('-', 1)[-1]
        if tail.isdigit():
            nums.append(int(tail))
    return max(nums, default=0) + 1


def assign_ipn(part):
    """给零件生成/修正 IPN。返回 True 表示 IPN 被改写。

    - 参数填全 → 正式码 {code}-{feat}（无流水号：同规格同码，天然去重）
    - 参数未填全 → 槽位码 !{code}-{槽位段}，未填槽位用 ? 占位；
      无论自动还是手工创建，只要含未设置的参数就以 ! 开头（待完善/复核）
    - 类别未绑参数模板 → 无规格流水码 {code}-S{serial}（正式码，非临时态）
    - 参数补齐后自动升级为正式码（! 同时消失）
    """
    code = get_category_code(part)
    if not code:
        return False
    cur = (part.IPN or '').strip()

    feat = feature_segment(part)
    if feat:
        target = f'{code}-{feat}'
        if cur == target:
            return False
        part.IPN = target
        logger.info('WeiTiMDM: %s -> %s', part.name, part.IPN)
        return True

    slots = feature_slots(part)
    if slots:
        # 走到这里说明 slots 含 ? 槽位（有未设置的参数）——
        # 无论自动还是手工创建，一律加 ! 前缀标记"待完善/复核"，
        # 参数补齐后 feature_segment 生效走上面分支，! 自动消失。
        target = f'!{code}-{slots}'
        if cur == target:
            return False
        part.IPN = target
        logger.info('WeiTiMDM: %s -> %s (参数未填全，待完善)',
                    part.name, part.IPN)
        return True

    # 类别未绑定参数模板 → 无规格零件，直接发正式流水码
    # 旧占位码 !{code}-Tnnnn 视为可迁移格式，重写成 S 码；
    # 已有 S 码或其他合规码则保持不变
    if cur and not cur.startswith(f'!{code}-T'):
        return False
    from part.models import Part
    prefix = f'{code}-S'
    nums = []
    for ipn in (Part.objects.filter(IPN__startswith=prefix)
                .values_list('IPN', flat=True)):
        tail = str(ipn)[len(prefix):]
        if tail.isdigit():
            nums.append(int(tail))
    new_ipn = f'{prefix}{max(nums, default=0) + 1:0{SERIAL_LEN}d}'
    if cur == new_ipn:
        return False
    part.IPN = new_ipn
    logger.info('WeiTiMDM: %s -> %s (无规格件，流水码)', part.name, part.IPN)
    return True


# ------------------------------------------------------------------
# 从描述(description)反解参数
# ------------------------------------------------------------------

# 是否启用「零件保存时自动从描述抽参数」
ENABLE_DESC_EXTRACT = True

RESISTANCE_RE = re.compile(r'(\d+(?:\.\d+)?\s*[kKmMrR]?)\s*(?:Ω|ohm|欧)?')

# 防止信号递归：正在处理的零件 pk
_processing = set()


def category_templates(cat):
    """返回类别关联的参数模板对象列表。

    沿类别树向上收集：祖先类别绑定的模板也算数（与 InvenTree 的
    "子类别继承父类别参数模板" 一致），因此模板只需绑在某一级父类别，
    其下各级子类别的零件都能用到。
    顺序：父类别在前、子类别在后（越靠上的字段排越前）；
    同一类别内按 pk 排序；同一模板重复绑定只取最靠上的一次。
    """
    from part.models import PartCategoryParameterTemplate
    # 类别链：顶层祖先 -> ... -> 自身
    chain = []
    c = cat
    seen_cat = set()
    while c is not None and getattr(c, 'pk', None) not in seen_cat:
        seen_cat.add(getattr(c, 'pk', None))
        chain.append(c)
        c = getattr(c, 'parent', None)
    chain.reverse()

    out = []
    seen_tpl = set()
    try:
        for cur in chain:
            for t in (PartCategoryParameterTemplate.objects
                      .filter(category=cur).order_by('pk')):
                tpl = getattr(t, 'template', None) or getattr(
                    t, 'parameter_template', None)
                if tpl is not None and tpl.pk not in seen_tpl:
                    seen_tpl.add(tpl.pk)
                    out.append(tpl)
    except Exception:
        pass
    return out


def _template_choices(template):
    try:
        return template.get_choices() or []
    except Exception:
        c = (getattr(template, 'choices', '') or '').strip()
        return [x.strip() for x in re.split(r'[,，;；\n]', c) if x.strip()]


def _strip_opt_code(s):
    """去掉选项前缀码: '01-SMD0603' -> 'SMD0603'。"""
    return re.sub(r'^\d+\s*-\s*', '', s).strip()


def match_from_choices(template, desc):
    """在描述里找该模板的某个选项，命中最长 token 的选项胜出。"""
    low = desc.lower()
    best = None
    for ch in _template_choices(template):
        bare = _strip_opt_code(ch)
        tokens = {ch, bare}
        m = re.search(r'([0-9]{3,4})', bare)
        if m:
            tokens.add(m.group(1))
        for t in tokens:
            t = t.strip()
            if len(t) >= 3 and t.lower() in low:
                if best is None or len(t) > best[0]:
                    best = (len(t), ch)
    return best[1] if best else None


def match_resistance(desc):
    """从描述抽阻值文本，如 '10K'。"""
    for m in RESISTANCE_RE.finditer(desc):
        tok = m.group(1).replace(' ', '')
        if re.match(r'^\d+(?:\.\d+)?[kKmMrR]?$', tok) and re.search(
                r'[kKmMrR]', tok):
            return tok.upper().replace('R', '')
    return None


def match_value(template, desc):
    """综合匹配：有选项走选项，阻值类走正则，否则 None。"""
    if _template_choices(template):
        return match_from_choices(template, desc)
    name = (template.name or '').lower()
    if any(k in name for k in ('阻值', '电阻值', 'resistance', '欧姆')):
        return match_resistance(desc)
    return None


def set_parameter(part, template, value):
    """给零件写一个参数值(新版泛型 Parameter,失败退回旧版 PartParameter)。"""
    try:
        from django.contrib.contenttypes.models import ContentType
        from common.models import Parameter
        ct = ContentType.objects.get_for_model(part)
        obj, created = Parameter.objects.get_or_create(
            model_type=ct, model_id=part.pk, template=template,
            defaults={'data': value})
        if not created and obj.data != value:
            obj.data = value
            obj.save()
        return True
    except Exception:
        try:
            from part.models import PartParameter
            obj, created = PartParameter.objects.get_or_create(
                part=part, template=template, defaults={'data': value})
            if not created and obj.data != value:
                obj.data = value
                obj.save()
            return True
        except Exception:
            logger.exception('WeiTiMDM: 写参数失败 part=%s tpl=%s',
                             part.pk, template.name)
            return False


def extract_params_from_description(part, overwrite=False):
    """从零件描述反解参数并写入。返回写入的 [(名称, 值)] 列表。"""
    desc = (getattr(part, 'description', '') or '').strip()
    if not desc or not getattr(part, 'category', None) or part.pk is None:
        return []
    tpls = category_templates(part.category)
    if not tpls:
        return []

    have = {}
    try:
        for p in part.parameters.all():
            have[p.template.name] = p
    except Exception:
        pass

    written = []
    for tpl in tpls:
        if tpl.name in have and not overwrite:
            continue
        val = match_value(tpl, desc)
        if not val:
            continue
        if set_parameter(part, tpl, val):
            written.append((tpl.name, val))
    if written:
        logger.info('WeiTiMDM: 从描述反解参数 part=%s -> %s', part.pk, written)
    return written


# ------------------------------------------------------------------
# 信号处理
# ------------------------------------------------------------------

def on_category_save(sender, instance, **kwargs):
    try:
        check_category_code_unique(instance)
        auto_code_category(instance)
    except ValidationError:
        raise  # 撞码必须阻断保存，不能被吞
    except Exception:
        logger.exception('WeiTiMDM: 类别自动编号失败')


def on_template_save(sender, instance, **kwargs):
    try:
        auto_code_template_options(instance)
    except Exception:
        logger.exception('WeiTiMDM: 模板选项自动编号失败')


def on_part_save(sender, instance, **kwargs):
    """零件保存前：导入字段兼容 → 尝试编码。"""
    try:
        fix_import_fields(instance)
        cat = getattr(instance, 'category', None)
        if cat is None:
            logger.warning('WeiTiMDM: 零件 "%s" 未指定类别，无法生成IPN',
                           instance.name)
        elif not get_category_code(instance):
            logger.warning(
                'WeiTiMDM: 零件 "%s" 挂在大类「%s」下（无3位小类码），'
                '无法生成IPN——请移到小类', instance.name, cat.name)
        assign_ipn(instance)
    except Exception:
        logger.exception('WeiTiMDM: 零件保存时编码失败 part=%s', instance.pk)


def on_part_postsave(sender, instance, **kwargs):
    """零件落库后：从描述反解参数（此时 pk 已存在，可建参数记录）。

    写入的每个参数会触发 on_parameter_save → 自动升级 IPN。
    用 _processing 防止「参数写入→IPN保存→再次 postsave」递归。
    """
    if not ENABLE_DESC_EXTRACT or instance.pk is None:
        return
    if instance.pk in _processing:
        return
    _processing.add(instance.pk)
    try:
        extract_params_from_description(instance)
    except Exception:
        logger.exception('WeiTiMDM: 描述反解参数失败 part=%s', instance.pk)
    finally:
        _processing.discard(instance.pk)


def on_parameter_save(sender, instance, **kwargs):
    """参数补填后触发编码——新建零件参数是后于零件落盘的。

    新版泛型 Parameter 用 content_object 指回零件；旧版 PartParameter 用 part。
    """
    try:
        part = getattr(instance, 'part', None) or getattr(
            instance, 'content_object', None)
        if part is None or not hasattr(part, 'IPN'):
            return
        if assign_ipn(part):
            part.save(update_fields=['IPN'])
    except Exception:
        logger.exception('WeiTiMDM: 参数保存时编码失败')


# ------------------------------------------------------------------
# 插件主体
# ------------------------------------------------------------------

class WeiTiMDMPlugin(UrlsMixin, UserInterfaceMixin, ValidationMixin,
                     EventMixin, ScheduleMixin, SettingsMixin,
                     InvenTreePlugin):
    """微体物料主数据插件：类别/选项自动编号 + IPN 自动生成 + BOM 一键导入
    + 订单联动闭环（SO→BO→PO）+ 齐套通知 + 排单优先级。"""

    NAME = 'WeiTiMDM'
    SLUG = 'weiti_mdm'
    TITLE = '微体物料主数据'
    DESCRIPTION = ('类别/参数选项自动编号、IPN自动生成({小类码}-{规格段}，'
                   '无规格件用S流水码)、导入归类兼容、描述反解参数、BOM一键导入、'
                   '订单联动与齐套通知')
    VERSION = '1.0.0'
    AUTHOR = '微体科技'

    # ---------------- 插件设置（管理后台→插件→WeiTiMDM→设置） ----------------

    SETTINGS = {
        'OF_ENABLE': {
            'name': '启用订单联动',
            'description': 'SO→BO→PO 缺料自动建单、齐套通知、优先级重算总开关',
            'validator': bool, 'default': True},
        'OF_WECOM_WEBHOOK': {
            'name': '企业微信机器人 Webhook',
            'description': '群机器人完整 webhook URL，留空则只发站内通知',
            'default': ''},
        'OF_BASE_URL': {
            'name': '站点地址兜底',
            'description': '通知链接的站点地址，如 http://192.168.1.188:1337；'
                           '留空则读全局设置 INVENTREE_BASE_URL',
            'default': ''},
        'OF_GROUP_PROD': {
            'name': '生产通知组',
            'description': '生产订单齐套时通知的 Django 用户组名',
            'default': '生产'},
        'OF_GROUP_SALES': {
            'name': '销售通知组',
            'description': '销售订单齐套时通知的 Django 用户组名',
            'default': '销售'},
        'OF_BUILD_DAYS': {
            'name': '默认生产周期(天)',
            'description': 'part.metadata 无 lead_time_days 时的自制周期兜底值',
            'validator': int, 'default': 7},
        'OF_PURCHASE_DAYS': {
            'name': '默认采购周期(天)',
            'description': 'SupplierPart.metadata 无 lead_time_days 时的采购周期兜底值',
            'validator': int, 'default': 14},
        'OF_MAKE_OR_BUY': {
            'name': '自制/外购兜底',
            'description': '可自制可外购且交期打平时：build=自制 purchase=外购',
            'choices': [('build', '自制优先'), ('purchase', '外购优先')],
            'default': 'purchase'},
        'OF_PRIO_DUE_W': {
            'name': '优先级·交期权重',
            'description': '逾期/临近交期的权重系数',
            'validator': float, 'default': 1.0},
        'OF_PRIO_VALUE_W': {
            'name': '优先级·金额权重',
            'description': '每万元订单金额的加分(上限30)',
            'validator': float, 'default': 1.0},
        'OF_PRIO_INH_W': {
            'name': '优先级·传导权重',
            'description': 'BO 继承其来源 SO 紧急度的权重(0~1)',
            'validator': float, 'default': 0.5},
    }

    # ---------------- 定时任务（每日重算优先级+齐套） ----------------

    SCHEDULED_TASKS = {
        'daily_recalc': {'func': 'task_daily_recalc', 'schedule': 'D'},
    }

    def task_daily_recalc(self, *args, **kwargs):
        """ScheduleMixin 每日任务入口。"""
        if not self.get_setting('OF_ENABLE'):
            return
        import orderflow
        orderflow.daily_recalc(self)

    # ---------------- 事件分发（EventMixin） ----------------

    def wants_process_event(self, event):
        import orderflow
        return event in orderflow.WATCHED_EVENTS

    def process_event(self, event, *args, **kwargs):
        """订单/库存事件 → 联动建单 + 齐套重查。"""
        if not self.get_setting('OF_ENABLE'):
            return
        import orderflow
        from order.models import PurchaseOrder, SalesOrder
        from build.models import Build
        pk = kwargs.get('id') or kwargs.get('order_id')
        try:
            if event == 'salesorder.issued':
                so = SalesOrder.objects.get(pk=pk)
                orderflow.on_sales_order_issued(self, so)
                orderflow.compute_priorities(self)
                orderflow.recheck_open_orders(self)
                orderflow.notify_if_kitted(self, so)
            elif event == 'build.issued':
                bo = Build.objects.get(pk=pk)
                orderflow.on_build_issued(self, bo)
                orderflow.compute_priorities(self)
                orderflow.recheck_open_orders(self)
                orderflow.notify_if_kitted(self, bo)
            elif event == 'salesorder.cancelled':
                so = SalesOrder.objects.get(pk=pk)
                orderflow.cancel_generated_children(so)
                orderflow.compute_priorities(self)
            elif event == 'build.cancelled':
                bo = Build.objects.get(pk=pk)
                orderflow.cancel_generated_children(bo)
                orderflow.compute_priorities(self)
            elif event in ('purchaseorderitem.received',
                           'stockitem.quantityupdated',
                           'stockitem.created_items',
                           'purchaseorder.completed',
                           'purchaseorder.cancelled',
                           'purchaseorder.placed',
                           'build.completed'):
                orderflow.recheck_open_orders(self)
        except Exception:
            logger.exception('WeiTiMDM: 事件 %s 处理失败(pk=%s)', event, pk)

    def validate_part_ipn(self, ipn, part):
        """手填 IPN 的格式校验（允许正式码和 ! 临时码）。"""
        s = str(ipn)
        if ipn and not (IPN_RE.match(s) or IPN_PROV_RE.match(s)):
            raise ValidationError(
                'IPN 格式须为 {小类码}-{规格段}（如 102-030110301）'
                '或 ! 开头的临时码')

    # --------------------------------------------------------------
    # BOM 一键导入页面 (UrlsMixin)
    # 访问: /plugin/weiti_mdm/bom-import/
    # --------------------------------------------------------------

    def setup_urls(self):
        from django.urls import path
        return [
            path('bom-import/', self.view_bom_import, name='bom-import'),
            path('bom-import.js', self.view_bom_js, name='bom-import-js'),
            path('bom-export/<int:pk>/', self.view_bom_export,
                 name='bom-export'),
            path('bom-export-tree/<int:pk>/', self.view_bom_export_tree,
                 name='bom-export-tree'),
            path('bom-export-multi/', self.view_bom_export_multi,
                 name='bom-export-multi'),
            path('supplier-import/', self.view_supplier_import,
                 name='supplier-import'),
            path('pending-parts/', self.view_pending_parts,
                 name='pending-parts'),
            path('schedule/', self.view_schedule_board,
                 name='schedule-board'),
        ]

    # ---------- 零件详情页"BOM导入"按钮（primary_action UI 特性） ----------

    def get_ui_primary_actions(self, request, context, **kwargs):
        """在零件详情页标题栏注入"BOM导入"按钮。

        前端 PageDetail 用 query param 'location' 传当前路由路径，
        零件详情页形如 /web/part/42/ —— 从中抠出 pk。
        context 是 QueryDict，取不到/不匹配就返回空（不出按钮）。
        """
        m = re.search(r'/part/(\d+)', str(context.get('location', '')))
        if not m:
            return []
        pk = m.group(1)
        actions = [{
            'key': 'weiti-bom-import',
            'title': 'BOM导入',
            'icon': 'ti:list-plus:outline',
            'options': {'color': 'teal'},
            'context': {'url': f'/plugin/weiti_mdm/bom-import/?parent={pk}'},
            'source': '/plugin/weiti_mdm/bom-import.js',
        }]
        # 有 BOM 行的零件才出"导出BOM"按钮
        try:
            from part.models import Part
            has_bom = Part.objects.filter(
                pk=pk, bom_items__isnull=False).exists()
        except Exception:
            has_bom = True
        if has_bom:
            actions.append({
                'key': 'weiti-bom-export',
                'title': '导出BOM',
                'icon': 'ti:file-export:outline',
                'options': {'color': 'orange'},
                'context': {'url': f'/plugin/weiti_mdm/bom-export/{pk}/'},
                'source': '/plugin/weiti_mdm/bom-import.js',
            })
            # 子件里有装配体 → 再加"导出BOM树"（多 tab 整树导出）
            try:
                from part.models import BomItem
                has_sub_assembly = BomItem.objects.filter(
                    part_id=pk, sub_part__assembly=True).exists()
            except Exception:
                has_sub_assembly = False
            if has_sub_assembly:
                actions.append({
                    'key': 'weiti-bom-tree',
                    'title': '导出BOM树',
                    'icon': 'ti:sitemap:outline',
                    'options': {'color': 'grape'},
                    'context': {'url': '/plugin/weiti_mdm/'
                                       f'bom-export-tree/{pk}/'},
                    'source': '/plugin/weiti_mdm/bom-import.js',
                })
        return actions

    # ---------- Spotlight 动作（Ctrl+K 搜索 → 整页跳插件页） ----------
    # 注意：navigation 特性的 options.url 只支持 SPA 内部路由，指向插件
    # Django 页面会被前端拼成 /web/plugin/... 而 404，故不用 navigation。

    def get_ui_spotlight_actions(self, request, context, **kwargs):
        """Spotlight 动作：executeAction 里 window.location.href 整页跳。"""
        if not (request.user and request.user.is_staff):
            return []
        src = '/plugin/weiti_mdm/bom-import.js'
        return [{
            'key': 'weiti-bom-import-action',
            'title': 'BOM导入',
            'description': '上传 BOM 文件：自动建零件、挂 BOM、导图片',
            'icon': 'ti:list-plus:outline',
            'context': {'url': '/plugin/weiti_mdm/bom-import/'},
            'source': src,
        }, {
            'key': 'weiti-supplier-import-action',
            'title': '供应商导入',
            'description': '零件关联供应商/制造商/SKU/价格',
            'icon': 'ti:building-store:outline',
            'context': {'url': '/plugin/weiti_mdm/supplier-import/'},
            'source': src,
        }, {
            'key': 'weiti-pending-parts-action',
            'title': '待完善编码零件',
            'description': '查看 IPN 以 ! 开头、参数待补齐的零件',
            'icon': 'ti:alert-circle:outline',
            'context': {'url': '/plugin/weiti_mdm/pending-parts/'},
            'source': src,
        }, {
            'key': 'weiti-schedule-action',
            'title': '排单看板',
            'description': '销售/生产/采购订单按优先级排序',
            'icon': 'ti:sort-descending:outline',
            'context': {'url': '/plugin/weiti_mdm/schedule/'},
            'source': src,
        }]

    # ---------- 首页 Dashboard 卡片 ----------

    def get_ui_dashboard_items(self, request, context, **kwargs):
        """仪表盘卡片：工具入口 + 待完善编码计数。"""
        if not (request.user and request.user.is_staff):
            return []
        pending = 0
        try:
            from part.models import Part
            pending = Part.objects.filter(IPN__startswith='!').count()
        except Exception:
            pass
        src = '/plugin/weiti_mdm/bom-import.js'
        pending_url = '/plugin/weiti_mdm/pending-parts/'
        return [{
            'key': 'weiti-mdm-tools',
            'title': '物料工具',
            'description': 'BOM / 供应商导入入口',
            'icon': 'ti:tools:outline',
            'options': {'width': 2, 'height': 1},
            'context': {
                'bom_url': '/plugin/weiti_mdm/bom-import/',
                'sup_url': '/plugin/weiti_mdm/supplier-import/',
                'pending_url': pending_url,
                'sched_url': '/plugin/weiti_mdm/schedule/'},
            'source': f'{src}:renderToolsCard',
        }, {
            'key': 'weiti-pending-count',
            'title': '待完善编码',
            'description': 'IPN 以 ! 开头的零件数量',
            'icon': 'ti:alert-circle:outline',
            'options': {'width': 1, 'height': 1},
            'context': {'count': pending, 'pending_url': pending_url},
            'source': f'{src}:renderPendingCard',
        }, {
            'key': 'weiti-urgent-orders',
            'title': '急单提醒',
            'description': '已逾期或7天内到期的开放订单',
            'icon': 'ti:alarm:outline',
            'options': {'width': 1, 'height': 1},
            'context': {'count': self._urgent_count(),
                        'sched_url': '/plugin/weiti_mdm/schedule/'},
            'source': f'{src}:renderUrgentCard',
        }]

    def _urgent_count(self):
        """逾期或7天内到期的开放订单总数（SO+BO+PO）。"""
        try:
            import InvenTree.helpers
            from datetime import timedelta
            from order.models import PurchaseOrder, SalesOrder
            from order.status_codes import (PurchaseOrderStatusGroups,
                                            SalesOrderStatusGroups)
            from build.models import Build
            from build.status_codes import BuildStatusGroups
            from django.db.models import Q
            soon = InvenTree.helpers.current_date() + timedelta(days=7)
            q = Q(target_date__isnull=False) & Q(target_date__lte=soon)
            n = (SalesOrder.objects.filter(
                    q, status__in=SalesOrderStatusGroups.OPEN).count()
                 + Build.objects.filter(
                    q, status__in=BuildStatusGroups.ACTIVE_CODES).count()
                 + PurchaseOrder.objects.filter(
                    q, status__in=PurchaseOrderStatusGroups.OPEN).count())
            return n
        except Exception:
            return 0

    # ---------- 待完善 IPN 零件清单页 ----------

    def view_pending_parts(self, request):
        """IPN 以 ! 开头的零件清单：IPN/名称/类别/参数填写进度。"""
        from django.http import HttpResponseForbidden
        if not (request.user.is_authenticated and request.user.is_staff):
            return HttpResponseForbidden('需要以员工(staff)身份登录')
        from part.models import Part
        rows = []
        for p in (Part.objects.filter(IPN__startswith='!')
                  .select_related('category').order_by('IPN', 'name')):
            try:
                ps = list(p.parameters.all())
                filled = sum(1 for x in ps if str(x.data or '').strip())
                prog = f'{filled}/{len(ps)}'
            except Exception:
                prog = '—'
            rows.append({
                'pk': p.pk, 'ipn': p.IPN, 'name': p.name,
                'cat': p.category.name if p.category else '',
                'prog': prog})
        ctx = {'plugin': self, 'parts': rows}
        return self._render(request, 'pending_parts.html', ctx)

    # ---------- 排单看板 ----------

    def view_schedule_board(self, request):
        """三类开放订单按优先级排序的三栏看板。"""
        from django.http import HttpResponseForbidden
        if not (request.user.is_authenticated and request.user.is_staff):
            return HttpResponseForbidden('需要以员工(staff)身份登录')
        import orderflow
        from order.models import PurchaseOrder, SalesOrder
        from order.status_codes import (PurchaseOrderStatusGroups,
                                        SalesOrderStatusGroups)
        from build.models import Build
        from build.status_codes import BuildStatusGroups

        # POST → 手动触发全量重算
        if request.method == 'POST':
            orderflow.daily_recalc(self)

        def prio(o):
            v = (o.metadata or {}).get(orderflow.META_PRIO)
            if v is None and hasattr(o, 'priority'):
                v = o.priority
            return float(v or 0)

        def kit(o):
            md = o.metadata or {}
            return md.get(orderflow.META_KITTED), md.get(orderflow.META_SRC, '')

        today = orderflow._today()

        def pack(objs, purl):
            out = []
            for o in objs:
                k, src = kit(o)
                out.append({'pk': o.pk, 'ref': o.reference,
                            'title': str(getattr(o, 'title', '') or ''),
                            'party': str(getattr(
                                getattr(o, 'customer', None)
                                or getattr(o, 'supplier', None)
                                or getattr(o, 'part', None), 'name',
                                '') or ''),
                            'target': o.target_date,
                            'overdue': bool(o.target_date
                                            and o.target_date < today),
                            'prio': prio(o), 'kitted': k, 'src': src,
                            'url': purl % o.pk})
            out.sort(key=lambda x: -x['prio'])
            return out

        ctx = {'plugin': self,
               'cols': [
                   ('销售订单', pack(SalesOrder.objects.filter(
                       status__in=SalesOrderStatusGroups.OPEN),
                       '/web/sales/sales-order/%s/')),
                   ('生产订单', pack(Build.objects.filter(
                       status__in=BuildStatusGroups.ACTIVE_CODES)
                       .select_related('part'),
                       '/web/manufacturing/build-order/%s/')),
                   ('采购订单', pack(PurchaseOrder.objects.filter(
                       status__in=PurchaseOrderStatusGroups.OPEN)
                       .select_related('supplier'),
                       '/web/purchasing/purchase-order/%s/')),
               ],
               'recalc': request.method == 'POST'}
        return self._render(request, 'schedule_board.html', ctx)

    def view_bom_js(self, request):
        """全部 UI 特性的 JS（单一模块，已被按钮链路验证可加载）。

        getFeature:     详情页按钮，feature.context 作为 args.serverContext
        executeAction:  Spotlight 动作，feature.context 作为 args.context
        renderXxxCard:  Dashboard 卡片，两参签名走前端 legacy DOM 路径：
                        fn(target, ctx)，feature.context 位于 ctx.context。
                        裸 JS（无 JSX 转译），用 :函数名 后缀指定入口。
        """
        from django.http import HttpResponse
        js = (
            'export function getFeature(args) {\n'
            '  if (args && args.serverContext && args.serverContext.url) {\n'
            '    window.location.href = args.serverContext.url;\n'
            '  }\n'
            '}\n'
            'export function executeAction(args) {\n'
            '  var u = args && args.context && args.context.url;\n'
            '  if (u) { window.location.href = u; }\n'
            '}\n'
            "function linkBtn(url, title, desc) {\n"
            "  return '<a href=\"' + url + '\" style=\"display:block;"
            "padding:10px 12px;border:1px solid #dee2e6;border-radius:8px;"
            "text-decoration:none;color:inherit\">'\n"
            "    + '<b>' + title + '</b>'\n"
            "    + '<div style=\"color:#888;font-size:12px;margin-top:2px\">'\n"
            "    + desc + '</div></a>';\n"
            "}\n"
            "export function renderToolsCard(target, ctx) {\n"
            "  if (!target) { return; }\n"
            "  var c = (ctx && ctx.context) || {};\n"
            "  target.innerHTML ="
            " '<div style=\"display:flex;flex-direction:column;gap:8px\">'\n"
            "    + linkBtn(c.bom_url, 'BOM 导入',\n"
            "        '上传 Excel/CSV：建零件、挂 BOM、导图片')\n"
            "    + linkBtn(c.sup_url, '供应商导入',\n"
            "        '零件关联供应商 / 制造商 / SKU / 价格')\n"
            "    + linkBtn(c.pending_url, '待完善编码',\n"
            "        'IPN 以 ! 开头的零件清单')\n"
            "    + linkBtn(c.sched_url, '排单看板',\n"
            "        '销售/生产/采购订单优先级排序')\n"
            "    + '</div>';\n"
            "}\n"
            "export function renderUrgentCard(target, ctx) {\n"
            "  if (!target) { return; }\n"
            "  var c = (ctx && ctx.context) || {};\n"
            "  var n = (c.count == null) ? '?' : c.count;\n"
            "  target.innerHTML ="
            " '<a href=\"' + c.sched_url + '\" style=\"text-decoration:none;"
            "color:inherit;display:flex;flex-direction:column;align-items:center;"
            "gap:6px;padding:8px\">'\n"
            "    + '<span style=\"font-size:38px;font-weight:700;line-height:1;"
            "color:#e8590c\">' + n + '</span>'\n"
            "    + '<span style=\"color:#888;font-size:13px\">"
            "个订单逾期或一周内到期，点击查看排单</span></a>';\n"
            "}\n"
            "export function renderPendingCard(target, ctx) {\n"
            "  if (!target) { return; }\n"
            "  var c = (ctx && ctx.context) || {};\n"
            "  var n = (c.count == null) ? '?' : c.count;\n"
            "  target.innerHTML ="
            " '<a href=\"' + c.pending_url + '\" style=\"text-decoration:none;"
            "color:inherit;display:flex;flex-direction:column;"
            "align-items:center;gap:6px;padding:8px\">'\n"
            "    + '<span style=\"font-size:38px;font-weight:700;"
            "line-height:1\">' + n + '</span>'\n"
            "    + '<span style=\"color:#888;font-size:13px\">"
            "个零件待完善编码，点击查看清单</span></a>';\n"
            "}\n")
        resp = HttpResponse(js, content_type='application/javascript')
        resp['Cache-Control'] = 'no-cache'
        return resp

    @staticmethod
    def _xlsx_response(data, filename):
        """xlsx 文件下载响应。"""
        from urllib.parse import quote
        from django.http import HttpResponse
        resp = HttpResponse(
            data,
            content_type='application/vnd.openxmlformats-'
                         'officedocument.spreadsheetml.sheet')
        resp['Content-Disposition'] = (
            f"attachment; filename*=UTF-8''{quote(filename)}")
        return resp

    def view_bom_export(self, request, pk):
        """导出单个零件 BOM 为带图片的 xlsx。"""
        from django.http import HttpResponseForbidden, HttpResponseNotFound
        import bom_export

        if not request.user.is_authenticated:
            return HttpResponseForbidden('需要登录')
        from part.models import Part
        part = Part.objects.filter(pk=pk).first()
        if not part:
            return HttpResponseNotFound('零件不存在')
        return self._xlsx_response(
            bom_export.build_bom_book([part]), f'{part.name}BOM.xlsx')

    def view_bom_export_tree(self, request, pk):
        """导出装配树：自身 + 所有下级装配体，每个零件一个工作表。"""
        from django.http import HttpResponseForbidden, HttpResponseNotFound
        import bom_export

        if not request.user.is_authenticated:
            return HttpResponseForbidden('需要登录')
        from part.models import Part
        part = Part.objects.filter(pk=pk).first()
        if not part:
            return HttpResponseNotFound('零件不存在')
        parts = bom_export.collect_assemblies(part)
        return self._xlsx_response(
            bom_export.build_bom_book(parts), f'{part.name}BOM树.xlsx')

    def view_bom_export_multi(self, request):
        """任意多零件合并导出：GET ?pks=1,2,3 → 每个零件一个工作表。"""
        from django.http import (
            HttpResponseBadRequest, HttpResponseForbidden)
        import bom_export

        if not request.user.is_authenticated:
            return HttpResponseForbidden('需要登录')
        pks = [int(x) for x in request.GET.get('pks', '').split(',')
               if x.strip().isdigit()]
        if not pks:
            return HttpResponseBadRequest('缺少 ?pks= 参数')
        from part.models import Part
        parts = list(Part.objects.filter(pk__in=pks))
        if not parts:
            return HttpResponseBadRequest('没有匹配到零件')
        return self._xlsx_response(
            bom_export.build_bom_book(parts), 'BOM合集.xlsx')

    def _resolve_parent(self, key):
        """按 pk / IPN / 名称 定位父零件。"""
        from part.models import Part
        key = (key or '').strip()
        if not key:
            return None
        if key.isdigit():
            p = Part.objects.filter(pk=int(key)).first()
            if p:
                return p
        return (Part.objects.filter(IPN=key).first()
                or Part.objects.filter(name=key).first())

    def _render(self, request, template_name, ctx):
        """从插件目录直接读模板渲染——不依赖 Django app 模板发现机制。

        插件目录不在 INSTALLED_APPS 里，render() 的模板加载器找不到
        templates/ 下的文件，所以这里手动读文件 + RequestContext 渲染
        （RequestContext 提供 csrf_token 等上下文处理器变量）。
        """
        from django.http import HttpResponse
        from django.template import RequestContext, Template
        path = os.path.join(PLUGIN_DIR, 'templates', template_name)
        with open(path, encoding='utf-8') as f:
            tpl = Template(f.read())
        return HttpResponse(tpl.render(RequestContext(request, ctx)))

    def view_bom_import(self, request):
        from django.http import HttpResponseForbidden
        import bom_import

        if not (request.user.is_authenticated and request.user.is_staff):
            return HttpResponseForbidden('需要以员工(staff)身份登录')

        ctx = {'plugin': self,
               'prefill': request.GET.get('parent', '')}
        action = request.POST.get('action', '') if request.method == 'POST' else ''

        # 步骤2：上传文件 → 解析所有工作表 → 落工作目录 → 渲染映射页
        if action == 'upload':
            import json
            import shutil
            import uuid
            from django.conf import settings as dj_settings

            f = request.FILES.get('file')
            if not f:
                ctx['error'] = '请提供 BOM 文件'
                return self._render(request, 'bom_upload.html', ctx)
            try:
                sheets = bom_import.parse_all_sheets(f)
            except Exception as e:
                ctx['error'] = f'文件解析失败: {e}'
                return self._render(request, 'bom_upload.html', ctx)
            if not sheets:
                ctx['error'] = '文件中没有可解析的工作表（需有表头和数据行）'
                return self._render(request, 'bom_upload.html', ctx)

            parent_key = (request.POST.get('parent') or '').strip()
            fixed_parent = None
            if parent_key:
                fixed_parent = self._resolve_parent(parent_key)
                if fixed_parent is None:
                    ctx['error'] = ('父零件无效：填写了 ID/IPN/名称'
                                    '但找不到对应零件')
                    return self._render(request, 'bom_upload.html', ctx)
                # 指定父零件 → 每个工作表各建一个子装配件挂到它下面
                if not fixed_parent.assembly:
                    fixed_parent.assembly = True
                    fixed_parent.save(update_fields=['assembly'])
            request.session['bom_fixed'] = (
                fixed_parent.pk if fixed_parent else '')

            # 工作目录：行数据(JSON) + 图片都落盘，session 只存元信息
            work = os.path.join(dj_settings.MEDIA_ROOT, 'tmp',
                                f'bom_{uuid.uuid4().hex[:12]}')
            os.makedirs(work, exist_ok=True)
            meta = []
            sheets_view = []
            for idx, s in enumerate(sheets):
                # 每个工作表按表名定位/待建一个（子）装配体，只探测不落库
                probe, pname = bom_import.probe_parent_part(s['sheet'])
                if not pname:
                    shutil.rmtree(work, ignore_errors=True)
                    ctx['error'] = (f'工作表「{s["sheet"]}」名称无法生成'
                                    '零件名，请手动指定父零件')
                    return self._render(request, 'bom_upload.html', ctx)
                if probe:
                    parent_pk, pipn, pa = probe.pk, probe.IPN or '', 'reused'
                    pname = probe.name
                else:
                    parent_pk, pipn, pa = None, '', 'will_create'
                srows = [{k: ('' if v is None else str(v))
                          for k, v in r.items()} for r in s['rows']]
                with open(os.path.join(work, f'sheet_{idx}.json'),
                          'w', encoding='utf-8') as fh:
                    json.dump({'headers': s['headers'], 'rows': srows},
                              fh, ensure_ascii=False)
                meta.append({
                    'sheet': s['sheet'], 'json': f'sheet_{idx}.json',
                    'parent_pk': parent_pk, 'pname': pname,
                    'pipn': pipn, 'auto': pa,
                    'rows': len(srows),
                    'imgs': bom_import.stash_images(
                        s['images'], work, prefix=f's{idx}_')})
                g = self._guess_columns(s['headers'])
                sheets_view.append({
                    'idx': idx, 'sheet': s['sheet'], 'headers': s['headers'],
                    'parent_pk': parent_pk, 'pname': pname,
                    'pipn': pipn, 'auto': pa, 'rows': len(srows),
                    'checked': True,
                    'preview': [[r.get(h, '') for h in s['headers']]
                                for r in srows[:3]],
                    'guess': g,
                    'spec_sel': [g['spec']] if g['spec'] else []})
            request.session['bom_work'] = work
            request.session['bom_sheets'] = meta
            ctx.update({'sheets_view': sheets_view,
                        'row_count': sum(m['rows'] for m in meta),
                        'parent': fixed_parent})
            return self._render(request, 'bom_map.html', ctx)

        # 步骤3/4：预览(dry-run) 或 提交（逐工作表执行）
        if action in ('preview', 'commit'):
            import json
            import shutil

            work = request.session.get('bom_work', '')
            meta = request.session.get('bom_sheets', [])
            if not work or not meta:
                ctx['error'] = '会话已过期，请重新上传'
                return self._render(request, 'bom_upload.html', ctx)
            sheet_data = []
            for m in meta:
                with open(os.path.join(work, m['json']),
                          encoding='utf-8') as fh:
                    sd = json.load(fh)
                sheet_data.append(
                    (m, sd['headers'], sd['rows'],
                     {int(k): v for k, v in (m.get('imgs') or {}).items()}))
            all_rows = [r for _m, _h, rows, _i in sheet_data for r in rows]

            fixed_pk = request.session.get('bom_fixed') or ''
            fixed_obj = (self._resolve_parent(str(fixed_pk))
                         if fixed_pk else None)

            # 每个工作表独立一套列映射：字段名带 _{idx} 后缀
            # 位号列不做映射配置：逐表按关键词自动识别
            ref_kw = ('位号', 'ref', 'designator')
            mappings = []
            for idx, (_m, hdrs, _rows, _i) in enumerate(sheet_data):
                mp = {k: request.POST.get(f'col_{k}_{idx}', '')
                      for k in ('name', 'qty', 'category')}
                mp['spec'] = [s for s in
                              request.POST.getlist(f'col_spec_{idx}') if s]
                mp['ref'] = next(
                    (h for h in hdrs
                     if any(k in str(h).lower() for k in ref_kw)), '')
                mappings.append(mp)

            # 逐表勾选导入：use_{idx} 未勾选则整表跳过
            enabled = [bool(request.POST.get(f'use_{idx}'))
                       for idx in range(len(sheet_data))]
            if not any(enabled):
                err = '请至少勾选一个要导入的工作表'
            elif any(not mp['name'] for idx, mp in enumerate(mappings)
                     if enabled[idx]):
                err = '每个待导入工作表都必须指定「组件名称」列'
            else:
                err = None
            if err:
                # 重渲染映射页，保留用户已选的列和勾选状态
                sheets_view = []
                for idx, (m, hdrs, rows, _i) in enumerate(sheet_data):
                    mp = mappings[idx]
                    sheets_view.append({
                        'idx': idx, 'sheet': m['sheet'], 'headers': hdrs,
                        'parent_pk': m['parent_pk'], 'pname': m['pname'],
                        'pipn': m['pipn'], 'auto': m['auto'],
                        'rows': len(rows), 'checked': enabled[idx],
                        'preview': [[r.get(h, '') for h in hdrs]
                                    for r in rows[:3]],
                        'guess': mp, 'spec_sel': mp['spec']})
                ctx.update({'sheets_view': sheets_view,
                            'row_count': len(all_rows),
                            'parent': fixed_obj,
                            'error': err})
                return self._render(request, 'bom_map.html', ctx)

            def collect_missing():
                """跨勾选的工作表收集缺失类别（按各自映射的类别列）。"""
                miss = []
                for idx, ((_m, _h, rows, _i), mp) in enumerate(
                        zip(sheet_data, mappings)):
                    if not enabled[idx]:
                        continue
                    for c in bom_import.missing_categories(
                            rows, mp['category']):
                        if c not in miss:
                            miss.append(c)
                return miss

            def run_all(dry):
                """逐工作表跑 run_import，汇总报告 + 按表分组明细。

                自动父零件在这里才真正创建：dry 时包在外层事务里随
                _Rollback 回滚；commit 时正常落库。
                指定了顶层父零件时，每个表名生成的子装配件再挂一行
                BOM 到该父零件。
                每个工作表一个事务：建父件 + 行导入 + link 挂载
                要么全成要么整表回滚，不留半提交状态。
                """
                from django.db import transaction
                rep = {'created': 0, 'reused': 0, 'bom_rows': 0,
                       'failed': 0, 'groups': []}

                def run_sheet(m, rows, imgs, mp):
                    pp = (self._resolve_parent(str(m['parent_pk']))
                          if m['parent_pk'] else None)
                    if pp is None:
                        pp, _act = bom_import.auto_parent_part(m['sheet'])
                    if pp is None:
                        return None
                    sub = bom_import.run_import(
                        pp, rows, mp, dry_run=dry, images=imgs,
                        category_map=run_all.category_map)
                    if fixed_obj is not None:
                        if fixed_obj.pk == pp.pk:
                            sub['failed'] += 1
                            sub['lines'].insert(0, {
                                'row': '—', 'name': m['pname'],
                                'action': 'error', 'cat': '',
                                'note': '子装配件与父零件同名，跳过挂载',
                                'ok': False})
                        else:
                            try:
                                ok, msg = bom_import.add_bom_item(
                                    fixed_obj, pp, '1', '',
                                    f'由工作表「{m["sheet"]}」导入', dry)
                            except Exception as e:
                                # 如循环挂载等校验错误：记为失败行，不中断整体
                                ok, msg = False, str(e)
                            if ok:
                                sub['bom_rows'] += 1
                                note = f'作为子装配挂到 {fixed_obj.name} x1'
                            else:
                                sub['failed'] += 1
                                note = (f'挂载到 {fixed_obj.name} 失败：{msg}'
                                        '（可能存在循环引用，请检查该零件的'
                                        ' BOM 子树中是否已包含父零件）')
                            sub['lines'].insert(0, {
                                'row': '—', 'name': m['pname'],
                                'action': 'link' if ok else 'error',
                                'cat': '', 'note': note, 'ok': ok})
                    return sub

                for idx, (m, _h, rows, imgs) in enumerate(sheet_data):
                    if not enabled[idx]:
                        rep['groups'].append({
                            'sheet': m['sheet'], 'parent_pk': m['parent_pk'],
                            'pname': m['pname'], 'pipn': m['pipn'],
                            'auto': m['auto'], 'skipped': True,
                            'sub': {'created': 0, 'reused': 0,
                                    'bom_rows': 0, 'failed': 0,
                                    'lines': []}})
                        continue
                    try:
                        with transaction.atomic():
                            sub = run_sheet(m, rows, imgs,
                                            mappings[idx])
                            if dry:
                                raise bom_import._Rollback()
                    except bom_import._Rollback:
                        pass
                    except Exception as e:
                        # 整表回滚（含自动父零件），不中断后续工作表
                        logger.exception(
                            'WeiTiMDM.bom: 工作表「%s」导入失败', m['sheet'])
                        sub = {'created': 0, 'reused': 0, 'bom_rows': 0,
                               'failed': max(1, len(rows)),
                               'lines': [{'row': '—', 'name': m['sheet'],
                                          'action': 'error', 'cat': '',
                                          'note': (f'导入失败，本表已整体'
                                                   f'回滚：{e}'),
                                          'ok': False}]}
                    if sub is None:
                        sub = {'created': 0, 'reused': 0, 'bom_rows': 0,
                               'failed': 1,
                               'lines': [{'row': '-', 'name': m['sheet'],
                                          'action': 'error', 'cat': '',
                                          'note': '父零件不存在',
                                          'ok': False}]}
                    rep['groups'].append({
                        'sheet': m['sheet'], 'parent_pk': m['parent_pk'],
                        'pname': m['pname'], 'pipn': m['pipn'],
                        'auto': m['auto'], 'sub': sub})
                    for k in ('created', 'reused', 'bom_rows', 'failed'):
                        rep[k] += sub[k]
                return rep
            run_all.category_map = {}

            if action == 'preview':
                report = run_all(dry=True)
                # 映射字段平铺成 hidden input，提交时原样带回
                # use_{idx} 只给勾选的表回传：未勾选即提交时跳过
                mfields = []
                for idx, mp in enumerate(mappings):
                    if enabled[idx]:
                        mfields.append((f'use_{idx}', '1'))
                    for k in ('name', 'qty', 'ref', 'category'):
                        mfields.append((f'col_{k}_{idx}', mp[k]))
                    for s in mp['spec']:
                        mfields.append((f'col_spec_{idx}', s))
                ctx.update({'report': report, 'sheets': meta,
                            'parent': fixed_obj,
                            'mfields': mfields, 'dry_run': True})
                # 缺失类别清单 → 报告页里给下拉让用户决策
                missing = collect_missing()
                if missing:
                    from part.models import PartCategory
                    tops_qs = PartCategory.objects.filter(
                        parent=None).order_by('name')
                    ctx['missing'] = missing
                    ctx['tops'] = [(c.pk, c.name) for c in tops_qs]
                    # 大类→小类的树，用于分级下拉：大类只作分组标题不可选
                    ctx['cat_tree'] = [
                        (t.name, [(c.pk, c.name) for c in
                                  PartCategory.objects.filter(
                                      parent=t).order_by('name')])
                        for t in tops_qs]
                return self._render(request, 'bom_report.html', ctx)

            # commit：先落实用户在预览页选的类别决策，再导 BOM
            missing = collect_missing()
            if missing:
                from part.models import PartCategory
                for i, raw in enumerate(missing):
                    dec = request.POST.get(f'catdec_{i}', 'skip')
                    try:
                        if dec.startswith('new:'):
                            p = PartCategory.objects.filter(
                                pk=int(dec[4:])).first()
                            nc = PartCategory(name=raw, parent=p)
                            nc.save()  # 插件信号自动编号 + 号段校验
                            run_all.category_map[raw] = nc.name
                        elif dec.startswith('map:'):
                            ec = PartCategory.objects.filter(
                                pk=int(dec[4:])).first()
                            if ec:
                                run_all.category_map[raw] = ec.name
                    except Exception:
                        logger.exception('WeiTiMDM.bom: 类别决策失败 %s', raw)
            report = run_all(dry=False)
            # 提交后清理工作目录（JSON + 图片）
            request.session.pop('bom_work', None)
            request.session.pop('bom_sheets', None)
            request.session.pop('bom_fixed', None)
            shutil.rmtree(work, ignore_errors=True)
            ctx.update({'report': report, 'sheets': meta,
                        'parent': fixed_obj, 'dry_run': False})
            return self._render(request, 'bom_report.html', ctx)

        # 步骤1：GET → 上传页
        return self._render(request, 'bom_upload.html', ctx)

    # --------------------------------------------------------------
    # 供应商/制造商导入页面
    # 访问: /plugin/weiti_mdm/supplier-import/
    # --------------------------------------------------------------

    def view_supplier_import(self, request):
        """供应商信息导入：零件标识(IPN/名称) → Company → SupplierPart
        (+ManufacturerPart + 价格)。流程同 BOM 导入：上传→映射→预览→提交。"""
        from django.http import HttpResponseForbidden
        import bom_import
        import supplier_import

        if not (request.user.is_authenticated and request.user.is_staff):
            return HttpResponseForbidden('需要以员工(staff)身份登录')

        ctx = {'plugin': self}
        action = request.POST.get('action', '') if request.method == 'POST' else ''

        # 步骤2：上传 → 逐表解析 → 映射页
        if action == 'upload':
            import json
            import uuid
            from django.conf import settings as dj_settings

            f = request.FILES.get('file')
            if not f:
                ctx['error'] = '请提供供应商文件'
                return self._render(request, 'sup_upload.html', ctx)
            try:
                sheets = bom_import.parse_all_sheets(f)
            except Exception as e:
                ctx['error'] = f'文件解析失败: {e}'
                return self._render(request, 'sup_upload.html', ctx)
            if not sheets:
                ctx['error'] = '文件中没有可解析的工作表（需有表头和数据行）'
                return self._render(request, 'sup_upload.html', ctx)

            work = os.path.join(dj_settings.MEDIA_ROOT, 'tmp',
                                f'sup_{uuid.uuid4().hex[:12]}')
            os.makedirs(work, exist_ok=True)
            meta, sheets_view = [], []
            for idx, s in enumerate(sheets):
                srows = [{k: ('' if v is None else str(v))
                          for k, v in r.items()} for r in s['rows']]
                with open(os.path.join(work, f'sheet_{idx}.json'),
                          'w', encoding='utf-8') as fh:
                    json.dump({'headers': s['headers'], 'rows': srows},
                              fh, ensure_ascii=False)
                meta.append({'sheet': s['sheet'],
                             'json': f'sheet_{idx}.json',
                             'rows': len(srows)})
                sheets_view.append({
                    'idx': idx, 'sheet': s['sheet'],
                    'headers': s['headers'], 'rows': len(srows),
                    'checked': True,
                    'preview': [[r.get(h, '') for h in s['headers']]
                                for r in srows[:3]],
                    'guess': supplier_import.guess_columns(s['headers'])})
            request.session['sup_work'] = work
            request.session['sup_sheets'] = meta
            ctx.update({'sheets_view': sheets_view,
                        'row_count': sum(m['rows'] for m in meta)})
            return self._render(request, 'sup_map.html', ctx)

        # 步骤3/4：预览(dry-run) 或 提交
        if action in ('preview', 'commit'):
            import json
            import shutil
            from django.db import transaction

            work = request.session.get('sup_work', '')
            meta = request.session.get('sup_sheets', [])
            if not work or not meta:
                ctx['error'] = '会话已过期，请重新上传'
                return self._render(request, 'sup_upload.html', ctx)
            currency = (request.POST.get('currency', '') or 'CNY').strip()

            sheet_data = []
            for m in meta:
                with open(os.path.join(work, m['json']),
                          encoding='utf-8') as fh:
                    sd = json.load(fh)
                sheet_data.append((m, sd['headers'], sd['rows']))

            # 每个工作表独立一套列映射
            mappings = [{k: request.POST.get(f'scol_{k}_{idx}', '')
                         for k in supplier_import.MAP_FIELDS}
                        for idx in range(len(sheet_data))]

            # 逐表勾选导入：use_{idx} 未勾选则整表跳过
            enabled = [bool(request.POST.get(f'use_{idx}'))
                       for idx in range(len(sheet_data))]
            if not any(enabled):
                err = '请至少勾选一个要导入的工作表'
            elif any(not mp['part'] or not mp['supplier']
                     for idx, mp in enumerate(mappings) if enabled[idx]):
                err = ('每个待导入工作表都必须指定'
                       '「零件标识」和「供应商」列')
            else:
                err = None
            if err:
                sheets_view = []
                for idx, (m, hdrs, rows) in enumerate(sheet_data):
                    sheets_view.append({
                        'idx': idx, 'sheet': m['sheet'], 'headers': hdrs,
                        'rows': m['rows'], 'checked': enabled[idx],
                        'preview': [[r.get(h, '') for h in hdrs]
                                    for r in rows[:3]],
                        'guess': mappings[idx]})
                ctx.update({'sheets_view': sheets_view,
                            'row_count': sum(m['rows'] for m in meta),
                            'currency': currency, 'error': err})
                return self._render(request, 'sup_map.html', ctx)

            dry = action == 'preview'
            report = {'created': 0, 'reused': 0, 'prices': 0,
                      'failed': 0, 'groups': []}
            for idx, (m, _h, rows) in enumerate(sheet_data):
                if not enabled[idx]:
                    report['groups'].append({
                        'sheet': m['sheet'], 'skipped': True,
                        'sub': {'created': 0, 'reused': 0, 'prices': 0,
                                'failed': 0, 'lines': []}})
                    continue
                try:
                    with transaction.atomic():
                        sub = supplier_import.run_import(
                            rows, mappings[idx], dry_run=dry,
                            currency=currency)
                        if dry:
                            raise bom_import._Rollback()
                except bom_import._Rollback:
                    pass
                except Exception as e:
                    # 整表回滚，不中断后续工作表
                    logger.exception(
                        'WeiTiMDM.sup: 工作表「%s」导入失败', m['sheet'])
                    sub = {'created': 0, 'reused': 0, 'prices': 0,
                           'failed': max(1, len(rows)),
                           'lines': [{'row': '—', 'name': m['sheet'],
                                      'action': 'error', 'cat': '',
                                      'note': (f'导入失败，本表已整体'
                                               f'回滚：{e}'),
                                      'ok': False}]}
                report['groups'].append({'sheet': m['sheet'], 'sub': sub})
                for k in ('created', 'reused', 'prices', 'failed'):
                    report[k] += sub[k]

            if dry:
                mfields = []
                for idx, mp in enumerate(mappings):
                    if enabled[idx]:
                        mfields.append((f'use_{idx}', '1'))
                    for k in supplier_import.MAP_FIELDS:
                        mfields.append((f'scol_{k}_{idx}', mp[k]))
                mfields.append(('currency', currency))
                ctx.update({'report': report, 'mfields': mfields,
                            'currency': currency, 'dry_run': True})
                return self._render(request, 'sup_report.html', ctx)

            request.session.pop('sup_work', None)
            request.session.pop('sup_sheets', None)
            shutil.rmtree(work, ignore_errors=True)
            ctx.update({'report': report, 'currency': currency,
                        'dry_run': False})
            return self._render(request, 'sup_report.html', ctx)

        return self._render(request, 'sup_upload.html', ctx)

    @staticmethod
    def _guess_columns(headers):
        """按列名关键词猜测默认映射。

        kw 的值是"优先级分组"：组内关键词同级，先扫完第一组
        所有表头都没命中，才退到下一组。如 category 先找「类别」，
        找不到再找「类型」。
        """
        guess = {'name': '', 'qty': '', 'ref': '', 'category': '', 'spec': ''}
        kw = {
            'name': [['物料名称', '名称', '组件', '零件', 'name']],
            'qty': [['数量', '用量', 'qty', 'quantity']],
            'ref': [['位号', '编号', '序号', 'ref', 'designator']],
            'category': [['类别', '分类', 'category'], ['类型', 'type']],
            'spec': [['规格', '型号', '规格型号', 'spec', 'description',
                      '描述']],
        }
        for field, groups in kw.items():
            for words in groups:
                for h in headers:
                    low = str(h).lower()
                    if any(w.lower() in low for w in words):
                        guess[field] = h
                        break
                if guess[field]:
                    break
        return guess


# 模块级挂信号：插件启用后随服务进程加载生效
_signals_connected = False
_signals_attempted = False


def _defer_ordercheck(task_path, obj_pk):
    """事务提交后把检查任务投给 worker（保证相关行已落库）。

    offload_task 默认 check_duplicates=True，同一次批量建行项目
    产生的多个相同任务会被合并为一个。
    """
    if not obj_pk:
        return
    from django.db import transaction
    from InvenTree.ready import isImportingData, isRebuildingData
    if isImportingData() or isRebuildingData():
        return

    def _go(t=task_path, i=obj_pk):
        try:
            from InvenTree.tasks import offload_task
            offload_task(t, i)
        except Exception:
            logger.exception('WeiTiMDM: 投递订单检查任务失败')
    transaction.on_commit(_go)


def on_order_line_save(sender, instance, **kwargs):
    """订单行项目保存（含新建/改数量）→ 异步缺口检查。"""
    try:
        cls_name = instance.__class__.__name__
        if cls_name == 'SalesOrderLineItem':
            _defer_ordercheck('orderflow.task_check_sales_order',
                              instance.order_id)
        elif cls_name == 'BuildLine':
            _defer_ordercheck('orderflow.task_check_build',
                              instance.build_id)
    except Exception:
        logger.exception('WeiTiMDM: 订单行信号处理失败')


def on_build_save(sender, instance, created, **kwargs):
    """新 BO 建立即检查（行项目可能尚未生成——BuildLine 信号兜底）。"""
    if not created:
        return
    try:
        _defer_ordercheck('orderflow.task_check_build', instance.pk)
    except Exception:
        logger.exception('WeiTiMDM: 生产单信号处理失败')


def _connect_signals():
    """各信号独立挂载，单个失败不拖垮整体。"""
    global _signals_connected
    if _signals_connected:
        return

    ok = True
    try:
        from part.models import Part, PartCategory
        pre_save.connect(on_category_save, sender=PartCategory,
                         dispatch_uid='weiti_mdm_category')
        pre_save.connect(on_part_save, sender=Part,
                         dispatch_uid='weiti_mdm_part')
        post_save.connect(on_part_postsave, sender=Part,
                          dispatch_uid='weiti_mdm_part_post')
    except Exception:
        ok = False
        logger.exception('WeiTiMDM: 零件/类别信号挂载失败')

    # 参数模型：新版 common.models.Parameter（泛型），旧版 part.models.PartParameter
    try:
        try:
            from common.models import Parameter as ParamModel
        except ImportError:
            from part.models import PartParameter as ParamModel
        post_save.connect(on_parameter_save, sender=ParamModel,
                          dispatch_uid='weiti_mdm_param')
    except Exception:
        ok = False
        logger.exception('WeiTiMDM: 参数信号挂载失败')

    # 参数模板模型：新版 common.models.ParameterTemplate
    try:
        try:
            from common.models import ParameterTemplate as TplModel
        except ImportError:
            from part.models import PartParameterTemplate as TplModel
        pre_save.connect(on_template_save, sender=TplModel,
                         dispatch_uid='weiti_mdm_template')
    except Exception:
        ok = False
        logger.exception('WeiTiMDM: 参数模板信号挂载失败(不影响其他功能)')

    # 订单联动信号：行项目保存即查缺料（不等到单下达）
    try:
        from order.models import SalesOrderLineItem
        from build.models import Build, BuildLine
        post_save.connect(on_order_line_save, sender=SalesOrderLineItem,
                          dispatch_uid='weiti_mdm_so_line')
        post_save.connect(on_order_line_save, sender=BuildLine,
                          dispatch_uid='weiti_mdm_build_line')
        post_save.connect(on_build_save, sender=Build,
                          dispatch_uid='weiti_mdm_build_new')
    except Exception:
        ok = False
        logger.exception('WeiTiMDM: 订单联动信号挂载失败')

    _signals_connected = ok
    if ok:
        logger.info('WeiTiMDM: 信号已挂载')


def _lazy_connect(sender, instance, **kwargs):
    """万能兜底接收器：第一次任何模型保存时才真正挂载。

    模块 import 时 Django app 可能还没就绪（挂载失败的高概率原因），
    不带 sender 的 pre_save 接收器不需要导入任何模型，绝对安全；
    第一次保存发生时 apps 必然已就绪，此时再挂真实信号。
    """
    global _signals_attempted
    if _signals_attempted:
        return
    _signals_attempted = True
    _connect_signals()
    pre_save.disconnect(_lazy_connect, dispatch_uid='weiti_mdm_lazy')

    # 本次保存顺带处理——Django 在 send 时已快照接收器列表，
    # 刚挂上的接收器收不到当前这次信号，手动补一刀
    try:
        from part.models import Part, PartCategory
        if isinstance(instance, PartCategory):
            on_category_save(sender, instance)
        elif isinstance(instance, Part):
            on_part_save(sender, instance)
    except Exception:
        pass


# 不指定 sender —— 对一切模型的保存生效，执行一次即自行卸载
pre_save.connect(_lazy_connect, weak=False, dispatch_uid='weiti_mdm_lazy')
_connect_signals()  # 先试一次；失败也无妨，懒挂载兜底
