"""供应商/制造商信息导入：解析列 → 匹配已有零件 → 建/更供应商关联。

数据落点（InvenTree 原生模型）：
  Company          —— 按名称去重，按需补 is_supplier / is_manufacturer
  ManufacturerPart —— 唯一键 (part, manufacturer, MPN)，可选
  SupplierPart     —— 唯一键 (part, supplier, SKU)
  SupplierPriceBreak —— 唯一键 (supplier_part, quantity)，同量更新价格

与 bom_import 同构：parse(复用) → 逐表映射 → dry-run 预览(事务回滚)
→ commit 落库。零件标识列同时支持 IPN 和名称（先 IPN 精确，再名称）。
"""

import logging
import re
from decimal import Decimal, InvalidOperation

import bom_import          # 复用 cell / parse_all_sheets / _Rollback

logger = logging.getLogger('inventree')


# ------------------------------------------------------------------
# 列名猜测（优先级分组，约定同 bom_import._guess_columns）
# ------------------------------------------------------------------

GUESS_KW = {
    'part': [['物料编码', '物料号', '料号', 'IPN', '内部零件编码', '零件编码'],
             ['物料名称', '名称', '零件', '物料', 'name', 'part']],
    'supplier': [['供应商', '供货商', 'supplier', 'vendor', 'seller'],
                 ['厂家', '厂商', 'company']],
    'mfr': [['品牌', '制造商', 'manufacturer', 'brand', '原厂']],
    'mpn': [['原厂型号', '制造商型号', '厂家型号', 'MPN', '型号']],
    'sku': [['SKU', '货号', '供应商料号', '订货号', '供方编码']],
    'price': [['含税价', '未税价', '单价', '价格', 'price', 'cost']],
    'qty': [['起订量', 'MOQ', '最小起订', '阶梯数量', '数量']],
    'note': [['备注', 'note', 'remark']],
}

MAP_FIELDS = ('part', 'supplier', 'mfr', 'mpn', 'sku', 'price', 'qty', 'note')


def guess_columns(headers):
    """按优先级分组扫描表头，返回 {field: 列名}。同 bom_import 的规则：
    组内关键词同级，全表扫完一组没命中才退到下一组。"""
    guess = {f: '' for f in MAP_FIELDS}
    for field, groups in GUESS_KW.items():
        for words in groups:
            for h in headers:
                low = str(h).lower()
                if any(w.lower() in low for w in words):
                    guess[field] = h
                    break
            if guess[field]:
                break
    return guess


# ------------------------------------------------------------------
# 实体解析 / 去重 / 建改
# ------------------------------------------------------------------

def find_part(text):
    """按标识定位零件：先 IPN 精确匹配，再按名称。
    返回 (part|None, err|None)。名称多匹配时报错提示改用 IPN。"""
    from part.models import Part
    t = (text or '').strip()
    if not t:
        return None, '零件标识为空'
    p = Part.objects.filter(IPN=t).first()
    if p:
        return p, None
    if not t.startswith('!'):
        # BOM 导入的待完善码以 ! 开头，表里的编码可能没带 !
        p = Part.objects.filter(IPN='!' + t).first()
        if p:
            return p, None
    qs = Part.objects.filter(name=t)
    n = qs.count()
    if n == 1:
        return qs.first(), None
    if n > 1:
        return None, f'名称「{t}」匹配到 {n} 个零件，请改用 IPN 列'
    return None, f'找不到零件「{t}」（按 IPN / 名称）'


def get_or_create_company(name, dry_run, manufacturer=False):
    """按名称查/建公司，复用时按需补 is_supplier/is_manufacturer 标记。
    返回 (company|None, action, note)。dry_run 下新建返回 (None,...)。
    """
    from company.models import Company
    n = (name or '').strip()[:100]
    if not n:
        return None, 'empty', '公司名为空'
    c = Company.objects.filter(name=n).first()
    if c:
        flags = []
        if not manufacturer and not c.is_supplier:
            c.is_supplier = True
            flags.append('is_supplier')
        if manufacturer and not c.is_manufacturer:
            c.is_manufacturer = True
            flags.append('is_manufacturer')
        note = f'复用公司 {n}'
        if flags:
            note += f'（补标记 {"/".join(flags)}）'
            if not dry_run:
                c.save(update_fields=flags)
        return c, 'reused', note
    if dry_run:
        return None, 'would_create', f'将新建公司 {n}'
    c = Company(name=n, description='由供应商导入创建',
                is_supplier=not manufacturer, is_manufacturer=manufacturer)
    c.save()
    return c, 'created', f'新建公司 {n}'


def upsert_mfr_part(part, mfr_company, mpn_text, dry_run):
    """制造商零件：唯一键 (part, manufacturer, MPN)。
    返回 (mfr_part|None, action)。"""
    from company.models import ManufacturerPart
    mpn = (mpn_text or '').strip()[:100] or None
    exist = ManufacturerPart.objects.filter(
        part=part, manufacturer=mfr_company, MPN=mpn).first()
    if exist:
        return exist, 'reused'
    if dry_run:
        return None, 'would_create'
    mp = ManufacturerPart(part=part, manufacturer=mfr_company, MPN=mpn)
    mp.save()
    return mp, 'created'


def _sku_default(part, sku_text):
    """SKU 兜底：未映射/为空时用零件 IPN（无 IPN 用名称/主键）。"""
    s = (sku_text or '').strip()
    if s:
        return s[:100]
    return (part.IPN or part.name or f'P{part.pk}')[:100]


def upsert_supplier_part(part, company, sku, mfr_part, note_text, dry_run):
    """供应商零件：唯一键 (part, supplier, SKU)。
    已存在则补 manufacturer_part / note；不存在则新建。
    返回 (sp|None, action, sku)。"""
    from company.models import SupplierPart
    exist = SupplierPart.objects.filter(
        part=part, supplier=company, SKU=sku).first()
    note = (note_text or '')[:100]
    if exist:
        if not dry_run:
            dirty = []
            if mfr_part and exist.manufacturer_part_id != mfr_part.pk:
                exist.manufacturer_part = mfr_part
                dirty.append('manufacturer_part')
            if note and not exist.note:
                exist.note = note
                dirty.append('note')
            if dirty:
                exist.save(update_fields=dirty)
        return exist, 'reused', sku
    if dry_run:
        return None, 'would_create', sku
    sp = SupplierPart(part=part, supplier=company, SKU=sku,
                      manufacturer_part=mfr_part, note=note or None)
    sp.save()
    return sp, 'created', sku


def upsert_price(supplier_part, qty_text, price_text, currency, dry_run):
    """单价 → SupplierPriceBreak(quantity, price)。
    同 quantity 已存在则更新价格。返回 (action, msg)，action:
    created/updated/reused/would_create/skip。"""
    from company.models import SupplierPriceBreak
    from moneyed import Money
    raw = re.sub(r'[^\d.\-]', '', str(price_text or ''))
    if not raw or raw in ('-', '.', '-.'):
        return 'skip', ''
    try:
        amount = Decimal(raw)
    except InvalidOperation:
        return 'error', f'单价无法解析：{price_text}'
    try:
        qty = Decimal(str(qty_text).strip()) if str(qty_text or '').strip() \
            else Decimal(1)
    except InvalidOperation:
        qty = Decimal(1)
    if qty <= 0:
        qty = Decimal(1)
    label = f'{currency} {amount} @{qty}'
    if dry_run:
        exist = (supplier_part.pricebreaks.filter(quantity=qty).exists()
                 if supplier_part is not None else False)
        return ('reused' if exist else 'would_create'), f'价格 {label}'
    pb = supplier_part.pricebreaks.filter(quantity=qty).first()
    if pb:
        pb.price = Money(amount, currency)
        pb.save()
        return 'updated', f'更新价格 {label}'
    SupplierPriceBreak.objects.create(
        part=supplier_part, quantity=qty, price=Money(amount, currency))
    return 'created', f'价格 {label}'


# ------------------------------------------------------------------
# 主流程
# ------------------------------------------------------------------

def run_import(rows, mapping, dry_run=True, currency='CNY'):
    """执行供应商导入。返回汇总 dict：
    {created(SupplierPart新建), reused(已有), prices(价格行), failed, lines}。
    """
    from django.db import transaction

    col_part = mapping.get('part')
    col_sup = mapping.get('supplier')
    col_mfr = mapping.get('mfr')
    col_mpn = mapping.get('mpn')
    col_sku = mapping.get('sku')
    col_price = mapping.get('price')
    col_qty = mapping.get('qty')
    col_note = mapping.get('note')

    report = {'created': 0, 'reused': 0, 'prices': 0, 'failed': 0,
              'lines': []}

    def process():
        # 公司对象缓存：同名公司在同一事务内只查/建一次
        cache = {}
        for i, row in enumerate(rows, start=1):
            ident = bom_import.cell(row, col_part)
            if not ident:
                continue
            try:
                part, err = find_part(ident)
                if not part:
                    raise ValueError(err)
                sup_name = bom_import.cell(row, col_sup)
                if not sup_name:
                    raise ValueError('供应商为空')

                key = ('sup', sup_name.strip())
                if key not in cache:
                    cache[key] = get_or_create_company(sup_name, dry_run)
                company, _ca, cnote = cache[key]

                # 制造商（可选）：公司角色 + ManufacturerPart
                mfr_part = None
                mnotes = []
                mfr_name = bom_import.cell(row, col_mfr)
                if mfr_name:
                    mkey = ('mfr', mfr_name.strip())
                    if mkey not in cache:
                        cache[mkey] = get_or_create_company(
                            mfr_name, dry_run, manufacturer=True)
                    mfr_co, _ma, mnote = cache[mkey]
                    mnotes.append(mnote)
                    if mfr_co is not None:
                        mfr_part, _mpa = upsert_mfr_part(
                            part, mfr_co,
                            bom_import.cell(row, col_mpn), dry_run)
                    else:
                        mnotes.append('将新建制造商零件')

                sku = _sku_default(part, bom_import.cell(row, col_sku))
                if company is not None:
                    sp, action, sku = upsert_supplier_part(
                        part, company, sku, mfr_part,
                        bom_import.cell(row, col_note), dry_run)
                else:
                    sp, action = None, 'would_create'

                if action in ('created', 'would_create'):
                    report['created'] += 1
                elif action == 'reused':
                    report['reused'] += 1

                notes = [cnote, *mnotes]
                if action == 'created':
                    notes.append(f'新建供应商件 {sup_name}|{sku}')
                elif action == 'reused':
                    notes.append(f'复用供应商件 {sup_name}|{sku}')
                else:
                    notes.append(f'将建供应商件 {sup_name}|{sku}')

                # 单价（可选）
                pval = bom_import.cell(row, col_price)
                if pval:
                    pa, pmsg = upsert_price(
                        sp, bom_import.cell(row, col_qty), pval,
                        currency, dry_run)
                    if pa == 'error':
                        raise ValueError(pmsg)
                    if pa in ('created', 'updated', 'would_create'):
                        report['prices'] += 1
                    if pmsg:
                        notes.append(pmsg)

                report['lines'].append({
                    'row': i, 'name': f'{part.IPN or ""} {part.name}'.strip(),
                    'action': action, 'cat': sup_name.strip(),
                    'note': '；'.join(n for n in notes if n), 'ok': True,
                })
            except Exception as e:
                logger.exception('WeiTiMDM.sup: 行 %s 失败', i)
                report['failed'] += 1
                report['lines'].append({
                    'row': i, 'name': ident, 'action': 'error',
                    'cat': '', 'note': str(e), 'ok': False,
                })

    if dry_run:
        try:
            with transaction.atomic():
                process()
                raise bom_import._Rollback()
        except bom_import._Rollback:
            pass
    else:
        with transaction.atomic():
            process()
    return report
