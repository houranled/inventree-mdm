"""IPN 自动编码插件（微体科技物料编码）

编码结构: {小类码}-{特征段}-{流水号}
  例: 201-03011-0001

规则说明（编码规则文档仅作格式参考，全部码值由系统自动分配，
           日常操作不需要查文档）:
  1. 类别码: 大类(顶层)手动定 1~8；小类创建时自动取「该号段下一个可用号」
     并自动改写名称前缀，如 在「2-结构类」下新建「螺丝」→ 自动变「201-螺丝」
  2. 选项码: 参数模板的选项值自动编号——只写 "金属膜/碳膜"，
     保存后自动变 "01-金属膜/02-碳膜"（已带码的保留并归一化位宽）
  3. 特征段: 按「类别参数模板的排列顺序」取每个参数值的前缀数字拼接
     - 参数值 "03-金属膜电阻" → 取 "03"
     - 参数值 "10K"/"4.7K" 等阻值 → 按 有效数字+10的幂 编码(10K→103, 4.7K→472)
     - 无数字前缀且非阻值 → 兜底 "00"
     - 参数没填全 → 发 !201-0001 式临时码（!=规格待补），补齐后自动升级正式码
  4. 流水号: 同「类别码-特征段」前缀下最大流水 +1，4位补零
  5. 导入兼容: Excel 旧类别名经 keywords 字段自动归类(alias.txt 别名表 +
     类别名匹配)；中文单位(个/片/只)自动换算为系统单位(pcs)
  6. 描述反解参数: 零件保存后(含导入)自动从 description 按类别参数模板匹配参数值,
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
from plugin.mixins import UrlsMixin, ValidationMixin

logger = logging.getLogger('inventree')

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
ALIAS_FILE = os.path.join(PLUGIN_DIR, 'alias.txt')

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
# 如 102-030110301；临时码(参数不全): !102-0001
IPN_RE = re.compile(r'^\d{3}-[0-9A-Za-z]{2,}$')
IPN_PROV_RE = re.compile(r'^!\d{3}-\d{4}$')

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
    """阻值编码: D1D2=两位有效数字, D3=10的幂。
    10K -> 103, 4.7K -> 472, 100Ω -> 101, 1M -> 105
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
    if exp < 0 or exp > 9:
        return None  # 超出单位数幂范围，交给兜底
    return f'{int(round(ohms))}{exp}'


def param_code(value):
    """参数值 -> 编码段：优先取前缀数字，其次按阻值规则，最后兜底。"""
    s = str(value).strip()
    m = OPT_CODE_RE.match(s)
    if m:
        return m.group(1)
    r = encode_resistance(s)
    return r if r else FALLBACK_CODE


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


def feature_segment(part):
    """按类别参数模板顺序拼接特征段。参数未填全返回 None。"""
    cat = getattr(part, 'category', None)
    if not cat:
        return None

    names = []
    try:
        from part.models import PartCategoryParameterTemplate
        tpls = (PartCategoryParameterTemplate.objects
                .filter(category=cat)
                .order_by('pk'))
        for t in tpls:
            tpl = getattr(t, 'template', None) or getattr(
                t, 'parameter_template', None)
            if tpl is not None:
                names.append(tpl.name)
    except Exception:
        names = []

    params = _part_params(part)
    if not names:
        # 类别没挂模板：按参数名排序拼接（保证确定性）
        names = sorted(params.keys())
    if not names:
        return None

    segs = []
    for name in names:
        val = params.get(name)
        if val is None or str(val).strip() == '':
            return None  # 参数没填全，先不编码
        segs.append(param_code(val))
    return ''.join(segs)


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
    - 参数不全且无码 → 临时码 !{code}-{serial}（规格待补标记，
      临时码仍带流水区分，因为此时没有特征段可分辨）
    - 临时码在参数补齐后自动升级为正式码
    """
    code = get_category_code(part)
    if not code:
        return False
    cur = (part.IPN or '').strip()
    feat = feature_segment(part)

    if not feat:
        # 参数不全 → 临时码（已有正式码的不降级，已有临时码的不动）
        if cur:
            return False
        prov_prefix = f'!{code}-'
        part.IPN = f'{prov_prefix}{next_serial(prov_prefix):0{SERIAL_LEN}d}'
        logger.info('WeiTiMDM: %s -> %s (临时码，规格未录全)',
                    part.name, part.IPN)
        return True

    target = f'{code}-{feat}'
    if cur == target:
        return False  # 已是合规 IPN，不动

    part.IPN = target
    logger.info('WeiTiMDM: %s -> %s', part.name, part.IPN)
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
    """返回类别关联的参数模板对象列表(按 pk 顺序)。"""
    from part.models import PartCategoryParameterTemplate
    out = []
    try:
        for t in (PartCategoryParameterTemplate.objects
                  .filter(category=cat).order_by('pk')):
            tpl = getattr(t, 'template', None) or getattr(
                t, 'parameter_template', None)
            if tpl is not None:
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

class WeiTiMDMPlugin(UrlsMixin, ValidationMixin, InvenTreePlugin):
    """微体物料主数据插件：类别/选项自动编号 + IPN 自动生成 + BOM 一键导入。"""

    NAME = 'WeiTiMDM'
    SLUG = 'weiti_mdm'
    TITLE = '微体物料主数据'
    DESCRIPTION = ('类别/参数选项自动编号、IPN自动生成({小类码}-{特征段}-{流水号})、'
                   '导入归类兼容、描述反解参数、BOM一键导入')
    VERSION = '0.2.0'
    AUTHOR = '微体科技'

    def validate_part_ipn(self, ipn, part):
        """手填 IPN 的格式校验（允许正式码和 ! 临时码）。"""
        s = str(ipn)
        if ipn and not (IPN_RE.match(s) or IPN_PROV_RE.match(s)):
            raise ValidationError(
                'IPN 格式须为 {小类码}-{规格段}（如 102-030110301）'
                '或 !xxx-xxxx 临时码')

    # --------------------------------------------------------------
    # BOM 一键导入页面 (UrlsMixin)
    # 访问: /plugin/weiti_mdm/bom-import/
    # --------------------------------------------------------------

    def setup_urls(self):
        from django.urls import path
        return [
            path('bom-import/', self.view_bom_import, name='bom-import'),
        ]

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
        path = os.path.join(PLUGIN_DIR, 'templates', 'weiti_mdm',
                            template_name)
        with open(path, encoding='utf-8') as f:
            tpl = Template(f.read())
        return HttpResponse(tpl.render(RequestContext(request, ctx)))

    def view_bom_import(self, request):
        from django.http import HttpResponseForbidden
        from weiti_mdm import bom_import

        if not (request.user.is_authenticated and request.user.is_staff):
            return HttpResponseForbidden('需要以员工(staff)身份登录')

        ctx = {'plugin': self}
        action = request.POST.get('action', '') if request.method == 'POST' else ''

        # 步骤2：上传文件 → 解析 → 存 session → 渲染映射页
        if action == 'upload':
            f = request.FILES.get('file')
            parent = self._resolve_parent(request.POST.get('parent'))
            if not f or parent is None:
                ctx['error'] = '请同时提供 BOM 文件和有效的父零件(ID/IPN/名称)'
                return self._render(request, 'bom_upload.html', ctx)
            try:
                headers, rows = bom_import.parse_file(f)
            except Exception as e:
                ctx['error'] = f'文件解析失败: {e}'
                return self._render(request, 'bom_upload.html', ctx)
            # session 存纯字符串,避免 JSON 序列化问题
            srows = [{k: ('' if v is None else str(v)) for k, v in r.items()}
                     for r in rows]
            request.session['bom_headers'] = headers
            request.session['bom_rows'] = srows
            request.session['bom_parent'] = parent.pk
            preview = [[r.get(h, '') for h in headers] for r in srows[:8]]
            ctx.update({'headers': headers, 'preview': preview,
                        'row_count': len(srows), 'parent': parent,
                        'guess': self._guess_columns(headers)})
            return self._render(request, 'bom_map.html', ctx)

        # 步骤3/4：预览(dry-run) 或 提交
        if action in ('preview', 'commit'):
            parent = self._resolve_parent(
                str(request.session.get('bom_parent', '')))
            rows = request.session.get('bom_rows', [])
            if parent is None or not rows:
                ctx['error'] = '会话已过期，请重新上传'
                return self._render(request, 'bom_upload.html', ctx)
            mapping = {k: request.POST.get('col_' + k, '')
                       for k in ('name', 'qty', 'ref', 'category', 'spec')}
            if not mapping['name']:
                hdrs = request.session.get('bom_headers', [])
                preview = [[r.get(h, '') for h in hdrs] for r in rows[:8]]
                ctx.update({'headers': hdrs, 'preview': preview,
                            'row_count': len(rows), 'parent': parent,
                            'error': '必须指定「组件名称」列', 'guess': mapping})
                return self._render(request, 'bom_map.html', ctx)
            dry = (action == 'preview')
            report = bom_import.run_import(parent, rows, mapping, dry_run=dry)
            ctx.update({'report': report, 'parent': parent,
                        'mapping': mapping, 'dry_run': dry})
            return self._render(request, 'bom_report.html', ctx)

        # 步骤1：GET → 上传页
        return self._render(request, 'bom_upload.html', ctx)

    @staticmethod
    def _guess_columns(headers):
        """按列名关键词猜测默认映射。"""
        guess = {'name': '', 'qty': '', 'ref': '', 'category': '', 'spec': ''}
        kw = {
            'name': ['物料名称', '名称', '组件', '零件', 'name'],
            'qty': ['数量', '用量', 'qty', 'quantity'],
            'ref': ['位号', '编号', '序号', 'ref', 'designator'],
            'category': ['类别', '分类', 'category'],
            'spec': ['规格', '型号', '规格型号', 'spec', 'description', '描述'],
        }
        for h in headers:
            low = str(h).lower()
            for field, words in kw.items():
                if guess[field]:
                    continue
                if any(w.lower() in low for w in words):
                    guess[field] = h
        return guess


# 模块级挂信号：插件启用后随服务进程加载生效
_signals_connected = False
_signals_attempted = False


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
