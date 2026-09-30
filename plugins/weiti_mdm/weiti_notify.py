"""通知分发：InvenTree 站内通知 + 企业微信群机器人 webhook。"""

import json
import logging
import urllib.request

logger = logging.getLogger('inventree')


# ------------------------------------------------------------------
# 收件人解析
# ------------------------------------------------------------------

def _owner_users(responsible):
    """订单 responsible 字段 → 用户列表。

    InvenTree 的 responsible 是 users.Owner（包 User 或 Group）。
    """
    if not responsible:
        return []
    try:
        return list(responsible.get_related_users(include_group=True))
    except Exception:
        pass
    owner = getattr(responsible, 'owner', None) or responsible
    from django.contrib.auth.models import Group, User
    if isinstance(owner, User):
        return [owner]
    if isinstance(owner, Group):
        return list(owner.user_set.all())
    return []


def _group_users(name):
    """按名字找 Django Group → 成员用户。"""
    if not name or not name.strip():
        return []
    from django.contrib.auth.models import Group
    users = []
    for g in Group.objects.filter(name=name.strip()):
        users.extend(g.user_set.all())
    return users


def collect_recipients(plugin, order, group_setting):
    """收件人 = 指定角色组全员 + 订单 responsible。"""
    users = {}
    for u in _owner_users(getattr(order, 'responsible', None)):
        users[u.pk] = u
    for u in _group_users(plugin.get_setting(group_setting)):
        users[u.pk] = u
    return list(users.values())


# ------------------------------------------------------------------
# 发送
# ------------------------------------------------------------------

def send_inapp(order, users, title, message):
    """站内通知（UI 通知铃）。"""
    if not users:
        return
    try:
        from common.notifications import trigger_notification
        trigger_notification(
            order, 'weiti.kitted', targets=users,
            context={'name': title, 'message': message},
            check_recent=False)
    except Exception:
        logger.exception('WeiTiMDM: 站内通知发送失败')


def send_wecom(plugin, content):
    """企业微信群机器人纯文本消息（不渲染 markdown，URL 直接可见）。"""
    url = (plugin.get_setting('OF_WECOM_WEBHOOK') or '').strip()
    if not url:
        return
    body = json.dumps({
        'msgtype': 'text',
        'text': {'content': content},
    }).encode('utf-8')
    try:
        req = urllib.request.Request(
            url, data=body,
            headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status != 200:
                logger.warning('WeiTiMDM: 企微 webhook 返回 %s', resp.status)
    except Exception:
        logger.exception('WeiTiMDM: 企微 webhook 发送失败')


def _order_url(order):
    """PUI 订单详情页路径（/web/ basename + 各模块前缀）。"""
    name = order.__class__.__name__
    seg = {'SalesOrder': 'sales/sales-order',
           'Build': 'manufacturing/build-order',
           'PurchaseOrder': 'purchasing/purchase-order'}.get(name, '')
    return '/web/%s/%s/' % (seg, order.pk) if seg else ''


def _site_base(plugin):
    """站点绝对地址：优先 InvenTree 全局 INVENTREE_BASE_URL，插件设置兜底。"""
    try:
        from common.settings import get_global_setting
        base = (get_global_setting('INVENTREE_BASE_URL') or '').strip()
        if base:
            return base.rstrip('/')
    except Exception:
        pass
    return (plugin.get_setting('OF_BASE_URL') or '').strip().rstrip('/')


def notify_kitted(plugin, order, kind):
    """订单齐套跳变 → 站内 + 企微。"""
    group_key = {'sales': 'OF_GROUP_SALES',
                 'build': 'OF_GROUP_PROD'}.get(kind, 'OF_GROUP_PROD')
    label = {'sales': '销售订单', 'build': '生产订单'}.get(kind, '订单')
    users = collect_recipients(plugin, order, group_key)
    ref = getattr(order, 'reference', str(order.pk))
    title = '%s %s 已齐套' % (label, ref)
    msg = '全部物料库存充足，可以下达/发货'
    send_inapp(order, users, title, msg)
    lines = [title, '订单：%s' % ref, '状态：已齐套，可开工']
    base = _site_base(plugin)
    if base:
        lines.append('链接：%s%s' % (base, _order_url(order)))
    send_wecom(plugin, '\n'.join(lines))
    logger.info('WeiTiMDM: %s 齐套通知已发 (收件 %d 人)', ref, len(users))


def notify_new_orders(plugin, source, created):
    """自动生成的下游订单 → 按类型分组通知：PO→采购组，BO→生产组。

    source 可为 None（定时巡检）或 Part（按BOM采购页手动触发）。
    """
    if not created:
        return
    pos = [o for o in created
           if o.__class__.__name__ == 'PurchaseOrder']
    bos = [o for o in created if o.__class__.__name__ == 'Build']
    if pos:
        _notify_created(plugin, source, pos, 'OF_GROUP_PUR', '采购单')
    if bos:
        _notify_created(plugin, source, bos, 'OF_GROUP_PROD', '生产单')


def _notify_created(plugin, source, orders, group_key, label):
    src = (getattr(source, 'reference', None)
           or getattr(source, 'IPN', None)
           or ('缺料巡检' if source is None else str(source)))
    users = collect_recipients(plugin, source, group_key)
    n = len(orders)
    refs = '、'.join(getattr(o, 'reference', str(o.pk)) for o in orders)
    # 子BO自动下达 → 文案报"已下达"；PENDING 的报"待确认"
    state, tail = '待确认', '请核对后下达。'
    if label == '生产单':
        try:
            from build.status_codes import BuildStatus
            if all(o.status == BuildStatus.PRODUCTION for o in orders):
                state, tail = '已下达', '已进入生产。'
        except Exception:
            pass
    title = '自动生成 %d 张 %s %s（来源：%s）' % (n, label, state, src)
    msg = '单号：%s' % refs
    target = orders[0] if orders else source
    send_inapp(target, users, title, msg)
    base = _site_base(plugin)
    lines = [title]
    for o in orders:
        sup = getattr(getattr(o, 'supplier', None), 'name', '')
        ln = '- %s%s' % (o.reference, '（%s）' % sup if sup else '')
        if base:
            ln += ' %s%s' % (base, _order_url(o))
        lines.append(ln)
    lines.append(tail)
    send_wecom(plugin, '\n'.join(lines))
    logger.info('WeiTiMDM: %s → 自动建单通知已发 %s (收件 %d 人)',
                src, refs, len(users))
