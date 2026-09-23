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

部署: 本目录放入 InvenTree 数据卷 plugins/ 下，重启容器后到
     管理员中心 → 插件 启用「IPN自动编码」。
"""

import logging
import os
import re

from django.core.exceptions import ValidationError
from django.db.models.signals import post_save, pre_save

from plugin import InvenTreePlugin
from plugin.mixins import ValidationMixin

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
# IPN 成品格式: 201-03011-0001；临时码(参数不全): !201-0001
IPN_RE = re.compile(r'^\d{3}-[0-9A-Za-z]+-\d{4}$')
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
        logger.warning('IPNEncoder: 父类别 "%s" 名称无大类码前缀，跳过自动编号',
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
            logger.info('IPNEncoder: 类别自动编号 "%s" -> "%s"', name, new_name)
            instance.name = new_name
            return

    logger.error('IPNEncoder: 大类 %s 码段已用尽，无法为 "%s" 编号', prefix, name)


def auto_code_template_options(instance):
    """参数模板选项自动编号：choices 里每行一个选项，
    已带 "NN-" 前缀的保留原码，未带的取下一个可用号。

    输入 "金属膜\\n碳膜" → 保存后变 "01-金属膜\\n02-碳膜"
    """
    raw = (instance.choices or '').strip()
    if not raw:
        return

    lines = [ln.strip() for ln in re.split(r'[\n;,，；]', raw) if ln.strip()]
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
            # 已带码 → 归一化成两位: "1-金属膜" -> "01-金属膜"
            rest = ln[m.end():].lstrip('- ').strip()
            out.append(f'{int(m.group(1)):02d}-{rest}')
            continue
        n = 1
        while n in used:
            n += 1
        used.add(n)
        out.append(f'{n:02d}-{ln}')

    new_choices = '\n'.join(out)
    if new_choices != raw:
        logger.info('IPNEncoder: 模板 %s 选项自动编号 -> %s',
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
        logger.exception('IPNEncoder: alias.txt 读取失败')
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
            logger.info('IPNEncoder: 按 keywords "%s" 自动归类 -> %s',
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


def feature_segment(part):
    """按类别参数模板顺序拼接特征段。参数未填全返回 None。"""
    cat = getattr(part, 'category', None)
    if not cat:
        return None

    try:
        from part.models import PartCategoryParameterTemplate
        tpls = (PartCategoryParameterTemplate.objects
                .filter(category=cat)
                .select_related('parameter_template')
                .order_by('pk'))
    except Exception:
        tpls = []

    params = {}
    try:
        for p in part.parameters.all():
            params[p.template.name] = p.data
    except Exception:
        return None

    names = [t.parameter_template.name for t in tpls]
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

    - 参数填全 → 正式码 {code}-{feat}-{serial}
    - 参数不全且无码 → 临时码 *{code}-{serial}（规格待补标记）
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
        logger.info('IPNEncoder: %s -> %s (临时码，规格未录全)',
                    part.name, part.IPN)
        return True

    target_prefix = f'{code}-{feat}-'
    if cur.startswith(target_prefix):
        return False  # 已有合规 IPN，不动

    serial = next_serial(target_prefix)
    part.IPN = f'{target_prefix}{serial:0{SERIAL_LEN}d}'
    logger.info('IPNEncoder: %s -> %s', part.name, part.IPN)
    return True


# ------------------------------------------------------------------
# 信号处理
# ------------------------------------------------------------------

def on_category_save(sender, instance, **kwargs):
    try:
        auto_code_category(instance)
    except Exception:
        logger.exception('IPNEncoder: 类别自动编号失败')


def on_template_save(sender, instance, **kwargs):
    try:
        auto_code_template_options(instance)
    except Exception:
        logger.exception('IPNEncoder: 模板选项自动编号失败')


def on_part_save(sender, instance, **kwargs):
    """零件保存前：导入字段兼容 → 尝试编码。"""
    try:
        fix_import_fields(instance)
        assign_ipn(instance)
    except Exception:
        logger.exception('IPNEncoder: 零件保存时编码失败 part=%s', instance.pk)


def on_parameter_save(sender, instance, **kwargs):
    """参数补填后触发编码——新建零件参数是后于零件落盘的。"""
    try:
        part = instance.part
        if assign_ipn(part):
            part.save(update_fields=['IPN'])
    except Exception:
        logger.exception('IPNEncoder: 参数保存时编码失败')


# ------------------------------------------------------------------
# 插件主体
# ------------------------------------------------------------------

class IPNEncoderPlugin(ValidationMixin, InvenTreePlugin):
    """微体科技物料编码插件：类别自动编号 + IPN 自动生成。"""

    NAME = 'IPNEncoder'
    SLUG = 'ipn_encoder'
    TITLE = 'IPN自动编码'
    DESCRIPTION = '按 {小类码}-{特征段}-{流水号} 规则自动生成 IPN，新建小类自动分配码段'
    VERSION = '0.1.0'
    AUTHOR = '微体科技'

    def validate_part_ipn(self, ipn, part):
        """手填 IPN 的格式校验（允许正式码和 ! 临时码）。"""
        s = str(ipn)
        if ipn and not (IPN_RE.match(s) or IPN_PROV_RE.match(s)):
            raise ValidationError(
                'IPN 格式须为 xxx-特征段-xxxx（如 201-03011-0001）'
                '或 !xxx-xxxx 临时码')


# 模块级挂信号：插件启用后随服务进程加载生效
_signals_connected = False


def _connect_signals():
    global _signals_connected
    if _signals_connected:
        return
    try:
        from part.models import (Part, PartCategory, PartParameter,
                                 PartParameterTemplate)
        pre_save.connect(on_category_save, sender=PartCategory,
                         dispatch_uid='ipn_encoder_category')
        pre_save.connect(on_template_save, sender=PartParameterTemplate,
                         dispatch_uid='ipn_encoder_template')
        pre_save.connect(on_part_save, sender=Part,
                         dispatch_uid='ipn_encoder_part')
        post_save.connect(on_parameter_save, sender=PartParameter,
                          dispatch_uid='ipn_encoder_param')
        _signals_connected = True
        logger.info('IPNEncoder: 信号已挂载')
    except Exception:
        logger.exception('IPNEncoder: 信号挂载失败')


_connect_signals()
