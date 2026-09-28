"""BOM 一键导入核心逻辑：解析文件 → 去重(名称+IPN特征) → 增量建零件 → 挂 BOM。

被插件的 UrlsMixin 视图调用。所有对 InvenTree 模型/插件函数的引用都在函数内
延迟导入，避免模块加载期的循环依赖与 AppRegistryNotReady。
"""

import io
import logging

logger = logging.getLogger('inventree')


# ------------------------------------------------------------------
# 文件解析
# ------------------------------------------------------------------

def parse_file(django_file):
    """解析上传的 xlsx/xls/csv，返回 (headers, rows)。

    headers: [列名...]
    rows:    [{列名: 值, ...}, ...]
    优先用 InvenTree 自带的 tablib，失败退回 openpyxl。
    """
    raw = django_file.read()
    name = (getattr(django_file, 'name', '') or '').lower()

    # 1) tablib（InvenTree 依赖，支持 xlsx/xls/csv）
    try:
        import tablib
        fmt = None
        if name.endswith('.csv'):
            fmt = 'csv'
        elif name.endswith('.xls'):
            fmt = 'xls'
        else:
            fmt = 'xlsx'
        data = tablib.Dataset()
        if fmt == 'csv':
            data.load(raw.decode('utf-8-sig'), format='csv')
        else:
            data.load(raw, format=fmt)
        headers = [str(h).strip() if h is not None else '' for h in data.headers]
        rows = []
        for row in data.dict:
            rows.append({str(k).strip(): row[k] for k in row})
        return headers, rows
    except Exception:
        logger.exception('WeiTiMDM.bom: tablib 解析失败，尝试 openpyxl')

    # 2) openpyxl 兜底（仅 xlsx）
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(raw), data_only=True, read_only=True)
    ws = wb.active
    rows_iter = ws.iter_rows(values_only=True)
    headers = [str(c).strip() if c is not None else '' for c in next(rows_iter)]
    rows = []
    for r in rows_iter:
        if all(c is None or str(c).strip() == '' for c in r):
            continue
        rows.append({headers[i]: r[i] if i < len(r) else None
                     for i in range(len(headers))})
    return headers, rows


def cell(row, col):
    """安全取单元格值 -> 去空白字符串。"""
    if not col:
        return ''
    v = row.get(col)
    if v is None:
        return ''
    return str(v).strip()


# ------------------------------------------------------------------
# 去重 / 特征计算
# ------------------------------------------------------------------

def compute_feature_from_text(category, spec_text):
    """从规格文本按类别模板算出 IPN 特征段。参数不齐返回 None。"""
    from weiti_mdm import category_templates, match_value, param_code
    tpls = category_templates(category)
    if not tpls:
        return None
    segs = []
    for tpl in tpls:
        val = match_value(tpl, spec_text or '')
        if not val:
            return None
        segs.append(param_code(val, tpl))
    return ''.join(segs)


def find_existing_part(name, category, spec_text):
    """按「名称 + IPN特征」查已有零件。

    - 同名且特征段相同(或都无特征) → 命中复用
    - 同名但特征不同 → 视为不同零件(返回 None → 上层新建)
    """
    from part.models import Part
    from weiti_mdm import feature_segment

    same_name = list(Part.objects.filter(name=name))
    if not same_name:
        return None

    incoming_feat = compute_feature_from_text(category, spec_text) if category else None

    for p in same_name:
        try:
            existing_feat = feature_segment(p)
        except Exception:
            existing_feat = None
        if incoming_feat == existing_feat:
            return p
    # 有同名但特征都不匹配：来料无法算特征时，再按规格文本比对——
    # 同名且规格相同才复用；规格不同视为不同零件(如线缆同名不同长度/接口)
    if incoming_feat is None:
        if not spec_text:
            return same_name[0]
        for p in same_name:
            if (p.description or '').strip() == spec_text.strip():
                return p
    return None


# ------------------------------------------------------------------
# 建零件 / 挂 BOM
# ------------------------------------------------------------------

def resolve_category(category_text):
    """用插件的归类逻辑把类别文本解析成类别对象（可能 None）。"""
    if not category_text:
        return None
    from weiti_mdm import match_category_from_text
    try:
        return match_category_from_text(category_text)
    except Exception:
        return None


def missing_categories(rows, col_cat):
    """收集类别列里匹配不到系统类别的文本（去重、保持出现顺序）。"""
    seen, out = set(), []
    for row in rows:
        raw = cell(row, col_cat)
        if raw and raw not in seen:
            seen.add(raw)
            if resolve_category(raw) is None:
                out.append(raw)
    return out


def get_or_create_part(name, spec_text, category_text, dry_run):
    """返回 (part_or_None, action, note)。action: reused/created/would_create。"""
    category = resolve_category(category_text)
    existing = find_existing_part(name, category, spec_text)
    if existing:
        return existing, 'reused', f'复用 #{existing.pk} {existing.IPN or ""}'.strip()

    if dry_run:
        cat_name = category.name if category else '(未匹配类别)'
        return None, 'would_create', f'将新建 [{cat_name}] {name}'

    from part.models import Part
    part = Part(
        name=name,
        description=spec_text or name,
        category=category,
        keywords=category_text or '',
        component=True,
        purchaseable=True,
    )
    # 保存触发插件信号 → 归类 + IPN + 描述反解参数。
    # 规格反解不全时，assign_ipn 会自动生成 !{code}-{槽位段} 的待完善码，
    # 这里无需再手工补 !。
    part.save()
    part.refresh_from_db()
    return part, 'created', f'新建 #{part.pk} {part.IPN or ""}'.strip()


def add_bom_item(parent, sub_part, quantity, reference, note, dry_run):
    """给父零件挂一个 BOM 行。返回 (ok, msg)。"""
    if dry_run:
        return True, f'将挂 {sub_part or "?"} x{quantity}'
    from part.models import BomItem
    try:
        qty = float(quantity) if quantity else 1.0
    except (ValueError, TypeError):
        qty = 1.0
    # 已存在同 (part, sub_part) 的 BOM 行则更新数量，否则新建
    existing = BomItem.objects.filter(part=parent, sub_part=sub_part).first()
    if existing:
        existing.quantity = qty
        if reference:
            existing.reference = reference
        existing.save()
        return True, f'更新BOM行 {sub_part} x{qty}'
    item = BomItem(
        part=parent, sub_part=sub_part, quantity=qty,
        reference=reference or '', note=note or '')
    item.save()
    return True, f'挂BOM行 {sub_part} x{qty}'


# ------------------------------------------------------------------
# 主流程
# ------------------------------------------------------------------

def run_import(parent_part, rows, mapping, dry_run=True, category_map=None):
    """执行 BOM 导入。

    mapping: {'name':列名, 'qty':列名, 'ref':列名, 'category':列名, 'spec':列名}
    category_map: {Excel原类别文本: 目标类别名}，用户在预览页确认的类别决策
    返回汇总 dict。
    """
    from django.db import transaction

    col_name = mapping.get('name')
    col_qty = mapping.get('qty')
    col_ref = mapping.get('ref')
    col_cat = mapping.get('category')
    # 规格列可为单列名或列名列表（多列按序拼接为规格文本）
    _spec = mapping.get('spec')
    spec_cols = _spec if isinstance(_spec, list) else ([_spec] if _spec else [])

    report = {
        'created': 0, 'reused': 0, 'bom_rows': 0,
        'failed': 0, 'lines': [],
    }

    def process():
        for i, row in enumerate(rows, start=1):
            name = cell(row, col_name)
            if not name:
                continue
            spec = '; '.join(v for v in
                             (cell(row, c) for c in spec_cols) if v)
            cat_raw = cell(row, col_cat)
            cat = (category_map or {}).get(cat_raw, cat_raw)
            qty = cell(row, col_qty) or '1'
            ref = cell(row, col_ref)

            try:
                part, action, note = get_or_create_part(
                    name, spec, cat, dry_run)
                if action in ('created', 'would_create'):
                    report['created'] += 1
                elif action == 'reused':
                    report['reused'] += 1

                bom_ok, bom_msg = True, ''
                if part is not None:
                    bom_ok, bom_msg = add_bom_item(
                        parent_part, part, qty, ref, spec, dry_run)
                    if bom_ok:
                        report['bom_rows'] += 1
                elif dry_run:
                    # 演练下新零件还没建，BOM 行也计入预期
                    report['bom_rows'] += 1
                    bom_msg = f'将挂 {name} x{qty}'

                report['lines'].append({
                    'row': i, 'name': name, 'action': action,
                    'note': note + (f'；{bom_msg}' if bom_msg else ''),
                    'ok': True,
                })
            except Exception as e:
                logger.exception('WeiTiMDM.bom: 行 %s 失败', i)
                report['failed'] += 1
                report['lines'].append({
                    'row': i, 'name': name, 'action': 'error',
                    'note': str(e), 'ok': False,
                })

    if dry_run:
        # 演练：用事务包住并强制回滚，确保绝不落库
        try:
            with transaction.atomic():
                process()
                raise _Rollback()
        except _Rollback:
            pass
    else:
        with transaction.atomic():
            process()

    return report


class _Rollback(Exception):
    """内部用：演练模式强制回滚。"""
    pass
