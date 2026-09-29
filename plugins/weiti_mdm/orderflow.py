"""订单联动闭环：齐套判定、SO→BO/PO 传导、优先级计算。

事件链:
  salesorder.issued          → 逐行查缺口 → 建 BO / 挂 PO 行
  build.issued               → BOM 行缺料 → 外购件挂 PO 行
                               (assembly 缺料由 Auto Create Builds 插件处理，
                                未启用时本插件兜底建子 BO)
  purchaseorderitem.received
  stockitem.quantityupdated
  stockitem.created_items
  build.completed            → 对未齐套的开放订单重查齐套

齐套口径: 零件 available_stock (总库存−全部分配) ≥ 尚未分配的需求量
标记:    order.metadata['weiti_kitted'] / ['weiti_shortages']
优先级:  Build.priority (原生字段)；SO/PO 写 metadata['weiti_priority']
"""

import logging
from datetime import timedelta
from decimal import Decimal

logger = logging.getLogger('inventree')

META_KITTED = 'weiti_kitted'
META_SHORT = 'weiti_shortages'
META_PRIO = 'weiti_priority'
META_SRC = 'weiti_source'

WATCHED_EVENTS = frozenset([
    'salesorder.issued',
    'salesorder.cancelled',
    'build.issued',
    'build.completed',
    'build.cancelled',
    'purchaseorder.placed',
    'purchaseorderitem.received',
    'purchaseorder.completed',
    'purchaseorder.cancelled',
    'stockitem.quantityupdated',
    'stockitem.created_items',
])


def get_plugin():
    """从注册表取本插件实例（信号/offload_task worker 里取设置用）。"""
    from plugin import registry
    try:
        return registry.get_plugin('weiti_mdm')
    except Exception:
        try:
            return registry.plugins.get('weiti_mdm')
        except Exception:
            return None


def _uncovered(part, need):
    """净缺口 = 需求 − 可用库存 − 在产 − 在途采购。

    净额口径使重复触发天然幂等：本插件刚建的 BO 计入
    quantity_being_built、PO 行计入 on_order，下一次触发缺口归零。
    """
    gap = Decimal(str(need)) - Decimal(str(part.available_stock or 0))
    for attr in ('quantity_being_built', 'on_order'):
        try:
            gap -= Decimal(str(getattr(part, attr) or 0))
        except Exception:
            pass
    return max(gap, Decimal(0))


# ------------------------------------------------------------------
# 齐套判定
# ------------------------------------------------------------------

def _today():
    import InvenTree.helpers
    return InvenTree.helpers.current_date()


def _line_need_so(line):
    """SO 行项目仍需从库存出的数量 = 数量 − 已发 − 已分配。"""
    need = Decimal(str(line.quantity)) - Decimal(str(line.shipped))
    try:
        need -= Decimal(str(line.allocated_quantity()))
    except Exception:
        pass
    return max(need, Decimal(0))


def _line_need_build(line):
    """BO 行仍需从库存出的数量 = 需求 − 已消耗 − 已分配。"""
    try:
        return Decimal(str(line.unallocated_quantity()))
    except Exception:
        return Decimal(0)


def check_sales_order_kitted(so):
    """返回缺料明细列表；空列表 = 齐套；None = 行未就绪不判定。"""
    if not so.lines.exists():
        return None
    shortages = []
    for line in so.lines.select_related('part').all():
        if not line.part:
            continue
        need = _line_need_so(line)
        if need <= 0:
            continue
        free = Decimal(str(line.part.available_stock or 0))
        if free < need:
            shortages.append({
                'ipn': line.part.IPN or '', 'name': line.part.name,
                'need': float(need), 'have': float(free),
                'gap': float(need - free)})
    return shortages


def check_build_kitted(build):
    """生产订单 BOM 行齐套判定（消耗品行直接视为齐套）。

    build_lines 为空但零件有 BOM → 行还没生成完，返回 None 不判定
    （防止建单瞬间的空行被误判为"齐套"发通知）。
    """
    if not build.build_lines.exists():
        try:
            if build.part.get_bom_items().exists():
                return None
        except Exception:
            pass
    shortages = []
    for line in (build.build_lines
                 .select_related('bom_item__sub_part').all()):
        if line.bom_item and getattr(line.bom_item, 'is_consumable', False):
            continue
        need = _line_need_build(line)
        if need <= 0:
            continue
        sub = line.bom_item.sub_part
        free = Decimal(str(sub.available_stock or 0))
        if free < need:
            shortages.append({
                'ipn': sub.IPN or '', 'name': sub.name,
                'need': float(need), 'have': float(free),
                'gap': float(need - free)})
    return shortages


def _meta(obj):
    return dict(obj.metadata or {})


def _write_kit_flag(order, shortages, kind):
    """写齐套标记；返回跳变类型。shortages=None 时跳过判定。"""
    if shortages is None:
        return 'not_ready'
    md = _meta(order)
    was = bool(md.get(META_KITTED))
    now = not shortages
    md[META_KITTED] = now
    md['weiti_kit_checked_at'] = str(_today())
    md[META_SHORT] = shortages
    order.metadata = md
    try:
        order.save(update_fields=['metadata'])
    except Exception:
        order.save()
    if now and not was:
        return 'became_kitted'
    if not now and was:
        return 'became_short'
    return None


def _is_pending(order):
    """草稿/待审态判定——此状态下的齐套跳变只记标记，不发通知。"""
    try:
        if order.__class__.__name__ == 'Build':
            from build.status_codes import BuildStatus
            return order.status == BuildStatus.PENDING.value
        from order.status_codes import SalesOrderStatus
        return order.status == SalesOrderStatus.PENDING.value
    except Exception:
        return False


def notify_if_kitted(plugin, order):
    """订单已齐套 → 发通知（用于 issued 时点补发草稿期已齐套的单）。"""
    md = order.metadata or {}
    if md.get(META_KITTED) and not _is_pending(order):
        import weiti_notify
        kind = 'build' if order.__class__.__name__ == 'Build' else 'sales'
        weiti_notify.notify_kitted(plugin, order, kind)


# ------------------------------------------------------------------
# make / buy 判定（按交期）
# ------------------------------------------------------------------

def _part_build_days(part, default):
    try:
        v = (part.metadata or {}).get('lead_time_days') \
            or (part.metadata or {}).get('build_time_days')
        return int(v) if v else default
    except Exception:
        return default


def _part_purchase_days(part, default):
    """取该零件各供应商交期的最小值（SupplierPart.metadata['lead_time_days']）。"""
    best = None
    for sp in part.supplier_parts.select_related('supplier').all():
        try:
            v = (sp.metadata or {}).get('lead_time_days')
            if v:
                d = int(v)
                best = d if best is None else min(best, d)
        except Exception:
            continue
    return best if best is not None else default


def choose_channel(plugin, part, need_by=None):
    """返回 'build' / 'purchase' / None。

    - 只能自制 → build；只能外购 → purchase
    - 两者皆可 → 按完工日期早晚比较；无数据按插件默认
    """
    can_build = bool(part.assembly)
    can_buy = bool(part.purchaseable) and part.supplier_parts.filter(
        supplier__active=True, supplier__is_supplier=True).exists()
    if can_build and not can_buy:
        return 'build'
    if can_buy and not can_build:
        return 'purchase'
    if not can_build and not can_buy:
        return None
    b_days = _part_build_days(part, int(plugin.get_setting('OF_BUILD_DAYS')))
    p_days = _part_purchase_days(
        part, int(plugin.get_setting('OF_PURCHASE_DAYS')))
    today = _today()
    b_done = today + timedelta(days=b_days)
    p_done = today + timedelta(days=p_days)
    if b_done != p_done:
        return 'build' if b_done < p_done else 'purchase'
    default = str(plugin.get_setting('OF_MAKE_OR_BUY') or 'purchase')
    return 'build' if default == 'build' else 'purchase'


# ------------------------------------------------------------------
# 订单联动
# ------------------------------------------------------------------

def _responsible_of(order):
    return getattr(order, 'responsible', None)


def _autocreate_active():
    """Auto Create Builds 内置插件启用时，assembly 缺料交给它处理。"""
    try:
        from plugin import registry
        p = registry.get_plugin('autocreatebuilds')
        return bool(p and p.is_active())
    except Exception:
        return True  # 查不到宁可不建，避免重复


def _create_build(part, qty, source_order, need_by):
    from build.models import Build
    from build.status_codes import BuildStatus
    is_build_src = source_order.__class__.__name__ == 'Build'
    return Build.objects.create(
        part=part,
        quantity=qty,
        title='由 %s 自动生成' % source_order.reference,
        parent=source_order if is_build_src else None,
        sales_order=(source_order.sales_order if is_build_src
                     else source_order
                     if source_order.__class__.__name__ == 'SalesOrder'
                     else None),
        responsible=_responsible_of(source_order),
        project_code=getattr(source_order, 'project_code', None),
        start_date=_today(),
        target_date=need_by,
        status=BuildStatus.PENDING,
        metadata={META_SRC: '%s:%s' % (
            source_order.__class__.__name__, source_order.pk)})


def _create_po_lines(plugin, items, source_order, need_by):
    """items: [{'part': Part, 'qty': Decimal}] → 按供应商分组建 PO。"""
    from order.models import PurchaseOrder, PurchaseOrderLineItem
    groups = {}
    for it in items:
        part, qty = it['part'], it['qty']
        sp = (part.supplier_parts
              .filter(supplier__active=True, supplier__is_supplier=True)
              .order_by('pk').first())
        if not sp:
            logger.warning('WeiTiMDM: %s 无可用供应商零件，无法自动建采购行',
                           part.IPN or part.name)
            continue
        groups.setdefault(sp.supplier_id, {'supplier': sp.supplier,
                                           'lines': []})['lines'].append(
            (sp, part, qty))
    created = []
    for g in groups.values():
        po = PurchaseOrder.objects.create(
            supplier=g['supplier'],
            responsible=_responsible_of(source_order),
            target_date=need_by,
            project_code=getattr(source_order, 'project_code', None),
            metadata={META_SRC: '%s:%s' % (
                source_order.__class__.__name__, source_order.pk)})
        for sp, part, qty in g['lines']:
            PurchaseOrderLineItem.objects.create(
                order=po, part=sp, quantity=qty,
                target_date=need_by,
                reference=str(part.IPN or ''))
        created.append(po)
        logger.info('WeiTiMDM: 自动生成采购单 %s (%d 行, 来源 %s)',
                    po.reference, len(g['lines']), source_order.reference)
    return created


def on_sales_order_issued(plugin, so):
    """SO 缺料 → 建 BO 或挂 PO（建单/下达/行变更任一时机均可调用）。"""
    to_build, to_buy = [], []
    for line in so.lines.select_related('part').all():
        if not line.part:
            continue
        need = _line_need_so(line)
        gap = _uncovered(line.part, need)
        if gap <= 0:
            continue
        need_by = line.target_date or so.target_date
        ch = choose_channel(plugin, line.part, need_by)
        if ch == 'build':
            try:
                bo = _create_build(line.part, gap, so, need_by)
                to_build.append(bo)
                logger.info('WeiTiMDM: %s → 自动生成生产单 %s (数量 %s)',
                            so.reference, bo.reference, gap)
            except Exception:
                logger.exception('WeiTiMDM: 为 %s 建生产单失败',
                                 line.part.name)
        elif ch == 'purchase':
            to_buy.append({'part': line.part, 'qty': gap})
        else:
            logger.warning('WeiTiMDM: %s 缺料 %s 但既不可自制也不可采购',
                           line.part.IPN or line.part.name, gap)
    if to_buy:
        try:
            _create_po_lines(plugin, to_buy, so, so.target_date)
        except Exception:
            logger.exception('WeiTiMDM: 为 %s 建采购单失败', so.reference)
    return to_build


def on_build_issued(plugin, build):
    """BO BOM 行缺料 → 外购件挂 PO；assembly 交给 Auto Create Builds
    （未启用时本插件兜底建子 BO）。建单/下达/行变更任一时机均可调用。"""
    to_buy, to_build = [], []
    skip_assembly = _autocreate_active()
    for line in build.build_lines.select_related(
            'bom_item__sub_part').all():
        if line.bom_item and getattr(line.bom_item, 'is_consumable', False):
            continue
        need = _line_need_build(line)
        if need <= 0:
            continue
        sub = line.bom_item.sub_part
        gap = _uncovered(sub, need)
        if gap <= 0:
            continue
        need_by = build.target_date
        if sub.assembly:
            if skip_assembly:
                continue
            ch = 'build'
        else:
            ch = choose_channel(plugin, sub, need_by)
        if ch == 'build':
            try:
                to_build.append(_create_build(sub, gap, build, need_by))
            except Exception:
                logger.exception('WeiTiMDM: 为 %s 建子生产单失败', sub.name)
        elif ch == 'purchase':
            to_buy.append({'part': sub, 'qty': gap})
    if to_buy:
        try:
            _create_po_lines(plugin, to_buy, build, build.target_date)
        except Exception:
            logger.exception('WeiTiMDM: 为 %s 建采购单失败', build.reference)
    return to_build


def cancel_generated_children(source):
    """上游单取消 → 取消其自动生成的下游单（仅限仍为 PENDING 的）。"""
    kind = source.__class__.__name__
    src_tag = '%s:%s' % (kind, source.pk)
    from build.models import Build
    from build.status_codes import BuildStatus
    from order.models import PurchaseOrder
    from order.status_codes import PurchaseOrderStatus

    if kind == 'SalesOrder':
        builds = Build.objects.filter(
            sales_order=source, status=BuildStatus.PENDING)
    elif kind == 'Build':
        builds = Build.objects.filter(
            parent=source, status=BuildStatus.PENDING)
    else:
        builds = Build.objects.none()
    for b in builds:
        try:
            b.cancel_build(None)
        except Exception:
            try:
                b.status = BuildStatus.CANCELLED
                b.save()
            except Exception:
                logger.exception('WeiTiMDM: 取消 %s 失败', b.reference)
        logger.info('WeiTiMDM: 随 %s 取消自动取消生产单 %s',
                    source.reference, b.reference)

    pos = PurchaseOrder.objects.filter(
        metadata__weiti_source=src_tag,
        status=PurchaseOrderStatus.PENDING.value)
    for po in pos:
        try:
            po.cancel_order()
        except Exception:
            try:
                po.status = PurchaseOrderStatus.CANCELLED.value
                po.save()
            except Exception:
                logger.exception('WeiTiMDM: 取消 %s 失败', po.reference)
        logger.info('WeiTiMDM: 随 %s 取消自动取消采购单 %s',
                    source.reference, po.reference)


# ------------------------------------------------------------------
# 优先级
# ------------------------------------------------------------------

def _due_score(target_date, today):
    """交期得分: 逾期 30+min(逾期天数,30)；30 天内线性 30→0。"""
    if not target_date:
        return 0
    d = (target_date - today).days
    if d < 0:
        return 30 + min(-d, 30)
    if d <= 30:
        return 30 - d
    return 0


def _order_value(order):
    try:
        tp = order.total_price
        return float(tp.amount) if tp else 0.0
    except Exception:
        return 0.0


def compute_priorities(plugin):
    """全量重算开放订单优先级。

    Build → 原生 priority 字段；SO/PO → metadata['weiti_priority']。
    """
    today = _today()
    due_w = float(plugin.get_setting('OF_PRIO_DUE_W'))
    val_w = float(plugin.get_setting('OF_PRIO_VALUE_W'))
    inh_w = float(plugin.get_setting('OF_PRIO_INH_W'))

    from order.models import PurchaseOrder, SalesOrder
    from order.status_codes import (PurchaseOrderStatusGroups,
                                    SalesOrderStatusGroups)
    from build.models import Build
    from build.status_codes import BuildStatusGroups

    def value_pts(order):
        return min(_order_value(order) / 10000.0 * val_w, 30.0)

    for so in SalesOrder.objects.filter(
            status__in=SalesOrderStatusGroups.OPEN):
        score = _due_score(so.target_date, today) * due_w + value_pts(so)
        md = _meta(so)
        md[META_PRIO] = round(score, 1)
        so.metadata = md
        try:
            so.save(update_fields=['metadata'])
        except Exception:
            so.save()

    for bo in Build.objects.filter(
            status__in=BuildStatusGroups.ACTIVE_CODES):
        own = _due_score(bo.target_date, today) * due_w
        inh = 0.0
        so = getattr(bo, 'sales_order', None)
        if so is not None:
            inh = _due_score(so.target_date, today) * due_w * inh_w
        score = own + inh + value_pts(bo)
        if bo.priority != int(round(score)):
            bo.priority = max(0, int(round(score)))
            try:
                bo.save(update_fields=['priority'])
            except Exception:
                bo.save()

    for po in PurchaseOrder.objects.filter(
            status__in=PurchaseOrderStatusGroups.OPEN):
        # 行最早需求日优先于单头 target_date
        dates = [po.target_date]
        for line in po.lines.all():
            if line.target_date:
                dates.append(line.target_date)
        dates = [d for d in dates if d]
        earliest = min(dates) if dates else None
        score = _due_score(earliest, today) * due_w + value_pts(po)
        md = _meta(po)
        md[META_PRIO] = round(score, 1)
        po.metadata = md
        try:
            po.save(update_fields=['metadata'])
        except Exception:
            po.save()


# ------------------------------------------------------------------
# 齐套重查（库存变动后）
# ------------------------------------------------------------------

def recheck_open_orders(plugin, notify=True):
    """对未标记齐套的开放 SO/BO 重查；跳变时发通知。"""
    import weiti_notify
    from order.models import SalesOrder
    from order.status_codes import SalesOrderStatusGroups
    from build.models import Build
    from build.status_codes import BuildStatusGroups

    for so in SalesOrder.objects.filter(
            status__in=SalesOrderStatusGroups.OPEN):
        try:
            transition = _write_kit_flag(
                so, check_sales_order_kitted(so), 'so')
            if (transition == 'became_kitted' and notify
                    and not _is_pending(so)):
                weiti_notify.notify_kitted(plugin, so, kind='sales')
        except Exception:
            logger.exception('WeiTiMDM: SO %s 齐套检查失败', so.reference)

    for bo in Build.objects.filter(
            status__in=BuildStatusGroups.ACTIVE_CODES):
        try:
            transition = _write_kit_flag(
                bo, check_build_kitted(bo), 'build')
            if (transition == 'became_kitted' and notify
                    and not _is_pending(bo)):
                weiti_notify.notify_kitted(plugin, bo, kind='build')
        except Exception:
            logger.exception('WeiTiMDM: BO %s 齐套检查失败', bo.reference)


def daily_recalc(plugin):
    """定时任务入口：优先级重算 + 齐套重查。"""
    try:
        compute_priorities(plugin)
    except Exception:
        logger.exception('WeiTiMDM: 优先级重算失败')
    recheck_open_orders(plugin)


# ------------------------------------------------------------------
# offload_task 入口（信号 on_commit 后投递，worker 里执行）
# ------------------------------------------------------------------

def task_check_sales_order(so_id):
    """SO 行项目保存后异步检查：缺料→建单；顺带刷新齐套标记。"""
    plugin = get_plugin()
    if not plugin or not plugin.get_setting('OF_ENABLE'):
        return
    from order.models import SalesOrder
    try:
        so = SalesOrder.objects.get(pk=so_id)
    except Exception:
        return
    try:
        on_sales_order_issued(plugin, so)
        transition = _write_kit_flag(
            so, check_sales_order_kitted(so), 'so')
        if transition == 'became_kitted' and not _is_pending(so):
            import weiti_notify
            weiti_notify.notify_kitted(plugin, so, kind='sales')
        compute_priorities(plugin)
    except Exception:
        logger.exception('WeiTiMDM: SO %s 异步检查失败', so_id)


def task_check_build(build_id):
    """BO 或其行项目保存后异步检查：缺料→建单；顺带刷新齐套标记。"""
    plugin = get_plugin()
    if not plugin or not plugin.get_setting('OF_ENABLE'):
        return
    from build.models import Build
    try:
        bo = Build.objects.get(pk=build_id)
    except Exception:
        return
    try:
        on_build_issued(plugin, bo)
        transition = _write_kit_flag(
            bo, check_build_kitted(bo), 'build')
        if transition == 'became_kitted' and not _is_pending(bo):
            import weiti_notify
            weiti_notify.notify_kitted(plugin, bo, kind='build')
        compute_priorities(plugin)
    except Exception:
        logger.exception('WeiTiMDM: BO %s 异步检查失败', build_id)
