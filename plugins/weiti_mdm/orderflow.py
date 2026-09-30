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
META_ROOT = 'weiti_root'

# 来源对象的 PUI 详情页路径（相对路径，写入 PO.link 后可点击跳回）
_SRC_URLS = {'Build': '/web/manufacturing/build-order/%s/',
             'SalesOrder': '/web/sales/sales-order/%s/',
             'PurchaseOrder': '/web/purchasing/purchase-order/%s/',
             'Part': '/web/part/%s/'}


def _src_ref(obj):
    """来源显示名：订单用 reference，零件用 IPN/名称。"""
    return (getattr(obj, 'reference', None)
            or getattr(obj, 'IPN', None) or str(obj))


def _src_tag(obj):
    """来源标签：'SalesOrder:12' 形式，写入 metadata 溯源。"""
    return '%s:%s' % (obj.__class__.__name__, obj.pk)


def _root_source(order):
    """最上游归属：沿 Build.parent 爬到顶；顶层 BO 有 sales_order 则根=SO。"""
    node, seen = order, {order.pk}
    while (node.__class__.__name__ == 'Build'
           and node.parent_id and node.parent_id not in seen):
        seen.add(node.parent_id)
        node = node.parent
    if node.__class__.__name__ == 'Build' and node.sales_order_id:
        return node.sales_order
    return node


def _resolve_tag(tag):
    """'SalesOrder:12' → 对象；不存在返回 None。"""
    try:
        kind, pk = str(tag).split(':', 1)
        if kind == 'SalesOrder':
            from order.models import SalesOrder
            return SalesOrder.objects.get(pk=pk)
        if kind == 'Build':
            from build.models import Build
            return Build.objects.get(pk=pk)
        if kind == 'PurchaseOrder':
            from order.models import PurchaseOrder
            return PurchaseOrder.objects.get(pk=pk)
        if kind == 'Part':
            from part.models import Part
            return Part.objects.get(pk=pk)
    except Exception:
        pass
    return None

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


def _committed_map():
    """{part_pk: 全部开放订单的已承诺需求合计}——聚合一次供级联逐行复用。"""
    try:
        return {r['part'].pk: r['need'] for r in collect_shortages()}
    except Exception:
        return {}


def _net_gap(part, need, committed=None):
    """净额口径缺口：供给池（库存+在产+在途）先偿还其它开放订单
    已承诺的需求，再看本行还差多少。

    committed 传 _committed_map() 的结果；committed 里已含本行需求
    （BuildLine 已生成的情形），故先减去本行需求才是"别人占的量"。
    committed=None 时退回 _uncovered 物理口径（兜底幂等）。
    """
    need = Decimal(str(need))
    if committed is None:
        return _uncovered(part, need)
    supply = Decimal(str(part.available_stock or 0))
    for attr in ('quantity_being_built', 'on_order'):
        try:
            supply += Decimal(str(getattr(part, attr) or 0))
        except Exception:
            pass
    others = max(Decimal(0), committed.get(part.pk, Decimal(0)) - need)
    free = max(Decimal(0), supply - others)
    return max(Decimal(0), need - free)


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


def _pick_supplier_part(part, qty):
    """按缺口数量取最低价的供应商零件；全部无价时按 pk 取第一个。"""
    sps = (part.supplier_parts
           .filter(supplier__active=True, supplier__is_supplier=True))
    best, best_price = None, None
    for sp in sps:
        try:
            p = sp.get_price(qty)
            amt = float(p.amount) if p is not None else None
        except Exception:
            amt = None
        if best is None or (amt is not None and
                            (best_price is None or amt < best_price)):
            best, best_price = sp, amt
    return best


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
    bo = Build.objects.create(
        part=part,
        quantity=qty,
        title='由 %s 自动生成' % _src_ref(source_order),
        parent=source_order if is_build_src else None,
        # 只挂顶层：SO 直建的 BO 记 sales_order；子 BO 不挂 SO
        #（SO页生产订单列表=用户真正订的整机，子树靠 parent+溯源标签）
        sales_order=(source_order
                     if source_order.__class__.__name__ == 'SalesOrder'
                     else None),
        responsible=_responsible_of(source_order),
        project_code=getattr(source_order, 'project_code', None),
        start_date=_today(),
        target_date=need_by,
        status=BuildStatus.PENDING,
        metadata={META_SRC: _src_tag(source_order),
                  META_ROOT: _src_tag(_root_source(source_order))})
    if is_build_src:
        # 子BO直接下达进生产：顶层BO发布即整棵树一键投产，
        # 其 issue 事件会继续向下级联建更深层的子单/PO。
        # issue_build 不可用/失败时手工转态+发事件兜底。
        prod = getattr(BuildStatus.PRODUCTION, 'value',
                       BuildStatus.PRODUCTION)
        try:
            try:
                bo.issue_build()
            except TypeError:
                bo.issue_build(None)
        except Exception:
            logger.exception('WeiTiMDM: %s issue_build 调用失败',
                             bo.reference)
        try:
            bo.refresh_from_db(fields=['status'])
        except Exception:
            pass
        if bo.status != prod:
            try:
                bo.status = prod
                if hasattr(bo, 'issuance_date'):
                    bo.issuance_date = _today()
                bo.save()
                try:
                    from plugin.events import trigger_event
                except ImportError:
                    from InvenTree.tasks import trigger_event
                trigger_event('build.issued', id=bo.pk)
            except Exception:
                logger.exception('WeiTiMDM: 子生产单 %s 自动下达失败，保持待生产',
                                 bo.reference)
    return bo


def _create_po_lines(plugin, items, source_order, need_by):
    """items: [{'part': Part, 'qty': Decimal}] → 按供应商分组建 PO。"""
    from order.models import PurchaseOrder, PurchaseOrderLineItem
    groups = {}
    for it in items:
        part, qty = it['part'], it['qty']
        # 预览页选定的供应商零件优先；否则按缺口数量自动取最低价
        sp = it.get('sp') or _pick_supplier_part(part, qty)
        if not sp:
            logger.warning('WeiTiMDM: %s 无可用供应商零件，无法自动建采购行',
                           part.IPN or part.name)
            continue
        try:
            price = sp.get_price(qty)
        except Exception:
            price = None
        groups.setdefault(sp.supplier_id, {'supplier': sp.supplier,
                                           'lines': []})['lines'].append(
            (sp, part, qty, price))
    created = []
    src_kind = source_order.__class__.__name__ if source_order else ''
    tpl = _SRC_URLS.get(src_kind, '')
    src_url = tpl % source_order.pk if tpl else ''
    src_ref = _src_ref(source_order) if source_order else '缺料巡检'
    for g in groups.values():
        md = {}
        if source_order is not None:
            md[META_SRC] = _src_tag(source_order)
            md[META_ROOT] = _src_tag(_root_source(source_order))
        po = PurchaseOrder.objects.create(
            supplier=g['supplier'],
            description='自动生成：为 %s 采购缺料' % src_ref,
            link=src_url,
            responsible=_responsible_of(source_order),
            target_date=need_by,
            project_code=getattr(source_order, 'project_code', None),
            metadata=md)
        for sp, part, qty, price in g['lines']:
            PurchaseOrderLineItem.objects.create(
                order=po, part=sp, quantity=qty,
                target_date=need_by,
                reference=str(part.IPN or ''),
                notes='来源 %s' % src_ref,
                purchase_price=price)
        created.append(po)
        logger.info('WeiTiMDM: 自动生成采购单 %s (%d 行, 来源 %s)',
                    po.reference, len(g['lines']), src_ref)
    if created:
        try:
            import weiti_notify
            weiti_notify.notify_new_orders(plugin, source_order, created)
        except Exception:
            logger.exception('WeiTiMDM: 采购单建单通知发送失败')
    return created


def on_sales_order_issued(plugin, so):
    """SO 缺料 → 建 BO 或挂 PO（建单/下达/行变更任一时机均可调用）。

    净额口径：已承诺给其它订单的在产/在途不算本单供给。"""
    to_build, to_buy = [], []
    committed = _committed_map()
    for line in so.lines.select_related('part').all():
        if not line.part:
            continue
        need = _line_need_so(line)
        gap = _net_gap(line.part, need, committed)
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
    if to_build:
        try:
            import weiti_notify
            weiti_notify.notify_new_orders(plugin, so, to_build)
        except Exception:
            logger.exception('WeiTiMDM: 生产单建单通知发送失败')
    return to_build


def on_build_issued(plugin, build):
    """BO BOM 行缺料 → 只自动建子 BO（assembly 交给 Auto Create
    Builds，未启用时本插件兜底）。可采购叶子件不再自动建 PO——
    一律等物料透查页人工确认供应商后生成。
    建单/下达/行变更任一时机均可调用。

    净额口径：已承诺给其它订单的在产/在途不算本单供给，
    避免"别人在产的料"被当成自由供给而漏建子单。"""
    to_build = []
    skip_assembly = _autocreate_active()
    committed = _committed_map()
    for line in build.build_lines.select_related(
            'bom_item__sub_part').all():
        if line.bom_item and getattr(line.bom_item, 'is_consumable', False):
            continue
        need = _line_need_build(line)
        if need <= 0:
            continue
        sub = line.bom_item.sub_part
        gap = _net_gap(sub, need, committed)
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
            # 叶子件 PO 一律等物料透查页人工确认（可选供应商）
            logger.info('WeiTiMDM: %s 缺 %s × %s，待透查页人工确认采购',
                        build.reference, sub.IPN or sub.name, gap)
    if to_build:
        try:
            import weiti_notify
            weiti_notify.notify_new_orders(plugin, build, to_build)
        except Exception:
            logger.exception('WeiTiMDM: 子生产单建单通知发送失败')
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
        # 子BO不再挂sales_order——取顶层BO并沿parent链收齐子孙，
        # 同步保证整棵树取消（不完全依赖 build.cancelled 事件级联）
        ids = set()
        for t in Build.objects.filter(sales_order=source):
            try:
                ids.update(t.get_descendants(include_self=True)
                           .values_list('pk', flat=True))
            except Exception:
                ids.add(t.pk)
        builds = Build.objects.filter(pk__in=ids,
                                      status=BuildStatus.PENDING)
    elif kind == 'Build':
        # 本BO及其子孙中仍是PENDING的
        ids = {source.pk}
        try:
            ids.update(source.get_descendants(include_self=False)
                       .values_list('pk', flat=True))
        except Exception:
            pass
        builds = Build.objects.filter(pk__in=ids,
                                      status=BuildStatus.PENDING)
    else:
        builds = Build.objects.none()
    for b in builds:
        if b.status != BuildStatus.PENDING:  # 可能已被级联取消
            continue
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

    # 直接来源或被标记为最上游归属的 PENDING PO 一并取消
    from django.db.models import Q
    pos = PurchaseOrder.objects.filter(
        Q(metadata__weiti_source=src_tag)
        | Q(metadata__weiti_root=src_tag),
        status=PurchaseOrderStatus.PENDING.value).distinct()
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
        # 最上游归属（SO 优先，其次顶层 BO）的交期传导
        root = _root_source(bo)
        if root is not bo:
            inh = _due_score(getattr(root, 'target_date', None),
                             today) * due_w * inh_w
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
        # 最上游归属订单的交期传导
        root = _resolve_tag((po.metadata or {}).get(META_ROOT, ''))
        if root is not None:
            score += _due_score(getattr(root, 'target_date', None),
                                today) * due_w * inh_w
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


def auto_shortage_scan(plugin):
    """每日缺料巡检：全局缺口 → 按供应商生成 PENDING 采购单。

    幂等：本次建的 PO 计入 on_order，下次巡检缺口归零，不会重复建单。
    只处理 purchaseable 且有供应商零件的叶子件；无供应商的行记日志跳过。
    """
    to_buy = []
    earliest = []
    for r in collect_shortages():
        p = r['part']
        if r['gap'] <= 0:
            continue
        if not getattr(p, 'purchaseable', False):
            logger.info('WeiTiMDM: 巡检跳过 %s（缺口 %s，不可采购）',
                        p.IPN or p.name, r['gap'])
            continue
        to_buy.append({'part': p, 'qty': r['gap']})
        if r.get('earliest'):
            earliest.append(r['earliest'])
    if not to_buy:
        return
    need_by = min(earliest) if earliest else _today()
    pos = _create_po_lines(plugin, to_buy, None, need_by)
    logger.info('WeiTiMDM: 缺料巡检生成 %d 张采购单', len(pos))


# ------------------------------------------------------------------
# offload_task 入口（信号 on_commit 后投递，worker 里执行）
# ------------------------------------------------------------------

def task_check_sales_order(so_id):
    """SO 行项目保存后异步检查：缺料→建单；顺带刷新齐套标记。

    草稿（待生产）状态的 SO 不建下游单——只刷新齐套标记；
    正式发布时 salesorder.issued 事件再触发建单级联。"""
    plugin = get_plugin()
    if not plugin or not plugin.get_setting('OF_ENABLE'):
        return
    from order.models import SalesOrder
    try:
        so = SalesOrder.objects.get(pk=so_id)
    except Exception:
        return
    try:
        if not _is_pending(so):
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
    """BO 或其行项目保存后异步检查：缺料→建单；顺带刷新齐套标记。

    草稿（待生产）状态的 BO 不建下游单——只刷新齐套标记；
    正式发布时 build.issued 事件再触发建单级联。"""
    plugin = get_plugin()
    if not plugin or not plugin.get_setting('OF_ENABLE'):
        return
    from build.models import Build
    try:
        bo = Build.objects.get(pk=build_id)
    except Exception:
        return
    try:
        if not _is_pending(bo):
            on_build_issued(plugin, bo)
        transition = _write_kit_flag(
            bo, check_build_kitted(bo), 'build')
        if transition == 'became_kitted' and not _is_pending(bo):
            import weiti_notify
            weiti_notify.notify_kitted(plugin, bo, kind='build')
        compute_priorities(plugin)
    except Exception:
        logger.exception('WeiTiMDM: BO %s 异步检查失败', build_id)


# ------------------------------------------------------------------
# 零件级"按BOM采购"
# ------------------------------------------------------------------

def collect_bom_leaves(part, qty):
    """逐层下钻 BOM 聚合叶子件需求（有下层BOM的子件视为制造，继续下钻）。

    返回 [{'part', 'need'}]，need 已按单层用量 × qty × 层级系数聚合。
    """
    # 无 BOM 的叶子件：自身即最底层需求（单件采购场景）
    if not part.bom_items.exists():
        return [{'part': part, 'need': Decimal(str(qty)), 'usage': ''}]

    agg = {}

    def walk(p, factor, path):
        for it in p.bom_items.select_related('sub_part').all():
            sub = it.sub_part
            if not sub or sub.pk in path:  # path 防御 BOM 环
                continue
            need = Decimal(str(it.quantity)) * Decimal(str(factor))
            if sub.bom_items.exists():
                walk(sub, need, path | {sub.pk})
            else:
                e = agg.setdefault(
                    sub.pk, {'part': sub, 'need': Decimal(0),
                             'notes': set(), 'refs': set()})
                e['need'] += need
                # BOM 行语义：note/参考位号 表达"用在哪、哪几颗"
                if getattr(it, 'note', None):
                    e['notes'].add(str(it.note).strip())
                if getattr(it, 'reference', None):
                    e['refs'].add(str(it.reference).strip())

    walk(part, Decimal(str(qty)), {part.pk})
    for e in agg.values():
        e['usage'] = '；'.join(sorted(x for x in
                                    (e['notes'] | e['refs']) if x))
    return list(agg.values())


def _source_demand_keys(source):
    """来源订单家族的需求键 {('Build'|'SalesOrder', pk)}——用于把
    本单自己的已承诺需求从"别人占的"中剔除（本页需求即这些承诺，
    重复扣会双算）。"""
    keys = set()
    if source is None:
        return keys
    kind = source.__class__.__name__
    if kind not in ('Build', 'SalesOrder'):
        return keys
    keys.add((kind, source.pk))
    try:
        if kind == 'Build':
            for pk in (source.get_descendants(include_self=True)
                       .values_list('pk', flat=True)):
                keys.add(('Build', pk))
            if getattr(source, 'sales_order_id', None):
                keys.add(('SalesOrder', source.sales_order_id))
        else:  # SalesOrder：其生产单（含子孙）的需求也算本家族的
            from build.models import Build
            for b in Build.objects.filter(sales_order=source):
                keys.add(('Build', b.pk))
                try:
                    for pk in (b.get_descendants(include_self=False)
                               .values_list('pk', flat=True)):
                        keys.add(('Build', pk))
                except Exception:
                    pass
    except Exception:
        pass
    return keys


def _linked_pos(source):
    """{part_pk: {'qty': 未收货量, 'pos': [(pk, ref)]}}——
    关联来源订单家族的开放采购行。

    Build：weiti_source/weiti_root 命中本BO及子孙BO标签；
    SalesOrder：标签指向该 SO。已收货部分入库存，只计未收量。
    """
    if source is None:
        return {}
    from django.db.models import Q
    kind = source.__class__.__name__
    if kind == 'Build':
        tags = ['Build:%s' % source.pk]
        try:
            tags = ['Build:%s' % b.pk
                    for b in source.get_descendants(include_self=True)]
        except Exception:
            pass
        q = (Q(metadata__weiti_source__in=tags)
             | Q(metadata__weiti_root__in=tags))
    elif kind == 'SalesOrder':
        tag = 'SalesOrder:%s' % source.pk
        q = (Q(metadata__weiti_source=tag)
             | Q(metadata__weiti_root=tag))
    else:
        return {}
    try:
        from order.models import PurchaseOrder, PurchaseOrderLineItem
        from order.status_codes import PurchaseOrderStatusGroups
        ids = set(PurchaseOrder.objects
                  .filter(q, status__in=PurchaseOrderStatusGroups.OPEN)
                  .values_list('pk', flat=True))
        if not ids:
            return {}
        agg = {}
        for li in (PurchaseOrderLineItem.objects
                   .filter(order_id__in=ids)
                   .select_related('order', 'part')):
            pk = li.part.part_id
            rec = agg.setdefault(pk, {'qty': Decimal(0), 'pos': {}})
            rec['qty'] += (Decimal(str(li.quantity))
                           - Decimal(str(li.received or 0)))
            rec['pos'][li.order_id] = li.order.reference
        return {pk: {'qty': v['qty'], 'pos': list(v['pos'].items())}
                for pk, v in agg.items()}
    except Exception:
        return {}


def _linked_builds(source):
    """{part_pk: {'qty': 未完成量, 'bos': [(pk, ref)]}}——
    关联来源订单家族的活跃生产单（产成品在产量）。

    Build：本BO及子孙BO（parent链），或打 weiti_source/root 标签者；
    SalesOrder：sales_order 外键或标签指向该 SO。
    """
    if source is None:
        return {}
    from django.db.models import Q
    kind = source.__class__.__name__
    if kind == 'Build':
        family = {source.pk}
        try:
            family = {b.pk for b in
                      source.get_descendants(include_self=True)}
        except Exception:
            pass
        tags = ['Build:%s' % pk for pk in family]
        q = (Q(pk__in=family)
             | Q(metadata__weiti_source__in=tags)
             | Q(metadata__weiti_root__in=tags))
    elif kind == 'SalesOrder':
        tag = 'SalesOrder:%s' % source.pk
        q = (Q(sales_order=source)
             | Q(metadata__weiti_source=tag)
             | Q(metadata__weiti_root=tag))
    else:
        return {}
    try:
        from build.models import Build
        from build.status_codes import BuildStatusGroups
        agg = {}
        for b in Build.objects.filter(
                q, status__in=BuildStatusGroups.ACTIVE_CODES):
            rem = (Decimal(str(b.quantity))
                   - Decimal(str(getattr(b, 'completed', 0) or 0)))
            if rem <= 0:
                continue
            rec = agg.setdefault(b.part_id, {'qty': Decimal(0),
                                             'bos': []})
            rec['qty'] += rem
            rec['bos'].append((b.pk, b.reference))
        return agg
    except Exception:
        return {}


def collect_bom_purchasables(part, qty, net_open=False, source=None):
    """在摊平叶子件基础上分出 需采购/跳过 两组。

    返回 (buy, skipped)：
      buy     = [{'part','need','qty','candidates','on_order','committed',
                  'own_in'}]
                qty=净缺口(扣可用/在产/在途)
      skipped = [{'part','need','reason','stock','on_order','building',
                  'committed','own_in'}]

    net_open=True 按净额扣减。source=来源订单（Build/SalesOrder）
    时用本单专属口径：
      在途 = 只计关联本单家族的开放采购行未收量；
      在产 = 只计关联本单家族的活跃生产单未完成量；
      已承诺 = 其它开放订单已提出的需求（本单家族剔除）；
      净缺口 = 需求 − 关联在途 − 关联在产 − max(0, 库存 − 已承诺)
    ——其它订单的承诺只从共享库存里扣，不吃本单专用供给。
    无 source 时退回全局口径（库存+全局在途+全局在产 − 全部已承诺）。
    """
    committed, own_in, own_bld = {}, {}, {}
    if net_open:
        excl = _source_demand_keys(source)
        for r in collect_shortages():
            tot = Decimal(str(r['need']))
            own = sum(Decimal(str(d['qty']))
                      for d in r.get('demands', [])
                      if (d.get('kind'), d.get('pk')) in excl)
            committed[r['part'].pk] = max(Decimal(0), tot - own)
        own_in = _linked_pos(source)
        own_bld = _linked_builds(source)
    skipped = []
    buy = []
    for e in collect_bom_leaves(part, qty):
        p = e['part']
        stock = Decimal(str(p.available_stock or 0))
        on_order_all = Decimal(str(getattr(p, 'on_order', 0) or 0))
        building_all = Decimal(
            str(getattr(p, 'quantity_being_built', 0) or 0))
        comm = committed.get(p.pk, Decimal(0))
        oin = own_in.get(p.pk, {'qty': Decimal(0), 'pos': []})
        obd = own_bld.get(p.pk, {'qty': Decimal(0), 'bos': []})
        if net_open:
            if source is not None:
                # 本单口径，不多不少刚好够买：
                # 自有供给（关联在途+关联在产）全额抵扣；
                # 自由供给 = 共享库存 + 非本单在途/在产，先偿还其它
                # 订单已承诺（它们的承诺会先吃掉这部分），剩余才抵扣
                free = max(Decimal(0),
                           stock + (on_order_all - oin['qty'])
                           + (building_all - obd['qty']) - comm)
                gap = e['need'] - oin['qty'] - obd['qty'] - free
                on_order = oin['qty']
                building = obd['qty']
            else:
                free = stock + on_order_all + building_all - comm
                gap = e['need'] - max(free, Decimal(0))
                on_order = on_order_all
                building = building_all
            gap = max(gap, Decimal(0))
        else:
            gap = _uncovered(p, e['need'])
            on_order = on_order_all
            building = building_all
        extra = {'stock': stock,
                 'stock_avail': max(Decimal(0), stock - comm),
                 'on_order': on_order,
                 'building': building, 'committed': comm,
                 'own_in': oin['qty'], 'own_pos': oin['pos'],
                 'own_bos': obd['bos'],
                 'on_order_all': on_order_all,
                 'building_all': building_all}
        if not p.purchaseable:
            skipped.append(dict(extra, part=p, need=e['need'],
                                reason='无下层BOM且未勾选可购买'))
            continue
        if gap > 0:
            e.update(extra)
            e['qty'] = gap
            e['candidates'] = _supplier_candidates(p, gap)
            buy.append(e)
        else:
            if source is not None:
                if (oin['qty'] + obd['qty']) > 0:
                    reason = '关联本单供给已覆盖'
                elif stock > comm:
                    reason = '库存已覆盖'
                else:
                    reason = '自由供给（非本单在途/在产）已覆盖'
            else:
                reason = '库存/在途已覆盖'
            skipped.append(dict(extra, part=p, need=e['need'],
                                reason=reason))
    return buy, skipped


def _supplier_candidates(part, qty):
    """供应商候选：按 qty 数量取价，价格升序（无报价排末尾）。

    返回 [{'sp_pk','sp','supplier','sku','price'}]。"""
    cands = []
    for sp in part.supplier_parts.filter(
            supplier__active=True, supplier__is_supplier=True):
        try:
            price = sp.get_price(qty)
        except Exception:
            price = None
        cands.append({'sp_pk': sp.pk, 'sp': sp,
                      'supplier': str(sp.supplier.name),
                      'sku': sp.SKU or '',
                      'price': price})
    cands.sort(key=lambda c: float(c['price'].amount)
               if c['price'] is not None else float('inf'))
    return cands


def create_pos_for_part(plugin, items, part, need_by=None):
    """零件级"按BOM采购"入口：source 为 Part，复用 _create_po_lines。"""
    return _create_po_lines(plugin, items, part, need_by or _today())


# ------------------------------------------------------------------
# 缺料总览 / 订单穿透
# ------------------------------------------------------------------

def collect_shortages():
    """聚合所有开放订单的零件需求缺口（采购员视角的缺料总览）。

    需求来源：开放 SO 行未分配量 + 开放 BO 行未分配量。
    返回 [{'part','need','gap','demands':[{kind,pk,ref,qty,date}],
           'stock','on_order','building','pos','bos'}]
    按最早需求日排序，只返回有需求或有缺口的零件。
    """
    from order.models import PurchaseOrderLineItem, SalesOrder
    from order.status_codes import (PurchaseOrderStatusGroups,
                                    SalesOrderStatusGroups)
    from build.models import Build, BuildLine
    from build.status_codes import BuildStatusGroups

    agg = {}  # part_pk -> entry

    def demand(part, qty, kind, pk, ref, date, root=None):
        if qty <= 0:
            return
        e = agg.setdefault(part.pk, {
            'part': part, 'need': Decimal(0), 'demands': []})
        e['need'] += qty
        d = {'kind': kind, 'pk': pk, 'ref': ref, 'qty': qty, 'date': date,
             'url': _SRC_URLS.get(kind, '') % pk if kind in _SRC_URLS else ''}
        if kind == 'Build':
            d['table_url'] = '/plugin/weiti_mdm/build-table/%s/' % pk
        # 需求的最上游归属（如 BO 需求归属 SO-0012），与直接来源相同则不标
        if (root is not None
                and (root.__class__.__name__, root.pk) != (kind, pk)):
            rk = root.__class__.__name__
            d['root_ref'] = _src_ref(root)
            d['root_url'] = (_SRC_URLS.get(rk, '') % root.pk
                             if rk in _SRC_URLS else '')
        e['demands'].append(d)

    for so in (SalesOrder.objects
               .filter(status__in=SalesOrderStatusGroups.OPEN)
               .select_related('customer')):
        for li in so.lines.select_related('part').all():
            if not li.part:
                continue
            demand(li.part, _line_need_so(li), 'SalesOrder', so.pk,
                   so.reference, li.target_date or so.target_date)

    roots = {}  # build_pk → 最上游订单（缓存，避免逐行爬链）
    for bl in (BuildLine.objects
               .filter(build__status__in=BuildStatusGroups.ACTIVE_CODES)
               .select_related('bom_item__sub_part', 'build')
               .all()):
        p = bl.bom_item.sub_part if bl.bom_item else None
        if not p:
            continue
        bo = bl.build
        if bo.pk not in roots:
            roots[bo.pk] = _root_source(bo)
        demand(p, _line_need_build(bl), 'Build', bl.build_id,
               bo.reference, bo.target_date, root=roots[bo.pk])

    rows = []
    for e in agg.values():
        p = e['part']
        e['stock'] = p.available_stock or 0
        e['on_order'] = getattr(p, 'on_order', 0) or 0
        e['building'] = getattr(p, 'quantity_being_built', 0) or 0
        e['gap'] = _uncovered(p, e['need'])
        e['pos'] = (PurchaseOrderLineItem.objects
                    .filter(part__part=p,
                            order__status__in=PurchaseOrderStatusGroups.OPEN)
                    .values_list('order__pk', 'order__reference')
                    .distinct())
        e['bos'] = (Build.objects
                    .filter(part=p, status__in=BuildStatusGroups.ACTIVE_CODES)
                    .values_list('pk', 'reference'))
        # 最低价供应商（按缺口数量取价）
        sp = _pick_supplier_part(p, e['gap'] if e['gap'] > 0 else e['need'])
        e['supplier'] = str(sp.supplier.name) if sp else ''
        try:
            e['price'] = sp.get_price(e['gap']) if sp else None
        except Exception:
            e['price'] = None
        e['demands'].sort(key=lambda d: (d['date'] is None, d['date']))
        e['earliest'] = next((d['date'] for d in e['demands']
                              if d['date']), None)
        rows.append(e)
    rows.sort(key=lambda e: (e['gap'] <= 0,       # 有缺口的排前面
                             e['earliest'] is None,
                             e['earliest']))
    return rows


def order_trace(order):
    """订单穿透树：SO→衍生BO/PO，BO→子BO/PO，逐层展开为扁平行列表。

    返回 [{depth, kind, pk, ref, status, url, kitted}]，kind ∈
    salesorder/build/purchaseorder，前端按 depth 缩进即可。
    """
    from build.models import Build
    from order.models import PurchaseOrder
    from django.db.models import Q

    rows = []
    labels = {'SalesOrder': '销售单', 'Build': '生产单',
              'PurchaseOrder': '采购单', 'Part': '零件'}

    entry_key = (order.__class__.__name__, order.pk)

    def emit(obj, kind, depth):
        url_tpl = _SRC_URLS.get(kind, '')
        rows.append({
            'depth': depth, 'd': min(depth, 8),
            'kind': kind, 'pk': obj.pk,
            'ref': _src_ref(obj),
            'label': labels.get(kind, kind),
            'status': (obj.get_status_display()
                       if hasattr(obj, 'get_status_display') else ''),
            'kitted': (obj.metadata or {}).get(META_KITTED),
            'current': (kind, obj.pk) == entry_key,
            'url': url_tpl % obj.pk if url_tpl else ''})

    def walk(obj, kind, depth, seen):
        key = (kind, obj.pk)
        if key in seen:
            return
        seen.add(key)
        emit(obj, kind, depth)
        if kind == 'SalesOrder':
            for b in Build.objects.filter(sales_order=obj):
                walk(b, 'Build', depth + 1, seen)
        if kind in ('SalesOrder', 'Build'):
            src = '%s:%s' % (kind, obj.pk)
            # 直接来源或最上游归属指向本单的 PO 都算下游
            for po in PurchaseOrder.objects.filter(
                    Q(metadata__weiti_source=src)
                    | Q(metadata__weiti_root=src)):
                walk(po, 'PurchaseOrder', depth + 1, seen)
        if kind == 'Build':
            for b in Build.objects.filter(parent=obj):
                walk(b, 'Build', depth + 1, seen)

    # 从任意节点进入都先爬到最上游根，再从根渲染整棵树
    #（PO/子BO 进入时也能看到完整 SO→BO→PO 链，入口节点高亮）
    root, root_kind = _trace_root(order)
    walk(root, root_kind, 0, set())
    return rows


def _trace_root(order):
    """穿透入口 → 追溯到最上游根节点，返回 (root_obj, root_kind)。

    PO：先看 metadata.weiti_root/weiti_source，解析出 SO/BO 再继续上爬；
    BO：沿 parent 爬顶，顶层有 SO 则根=SO；
    SO：自身即根；手工单无 metadata 时以自身为根。
    """
    kind = order.__class__.__name__
    if kind == 'PurchaseOrder':
        md = order.metadata or {}
        up = _resolve_tag(md.get('weiti_root') or md.get('weiti_source') or '')
        if up is None:
            return order, kind          # 手工 PO：无上游，根即自身
        order, kind = up, up.__class__.__name__
    if kind == 'Build':
        root = _root_source(order)      # 爬 parent 链，顶层有 SO 则返回 SO
        return root, root.__class__.__name__
    return order, kind
