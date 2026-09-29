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
    """企业微信群机器人 markdown 消息。"""
    url = (plugin.get_setting('OF_WECOM_WEBHOOK') or '').strip()
    if not url:
        return
    body = json.dumps({
        'msgtype': 'markdown',
        'markdown': {'content': content},
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
    content = '### %s\n> 订单：**%s**\n> 状态：已齐套，可开工' % (title, ref)
    base = _site_base(plugin)
    if base:
        content += '\n> [点击查看订单](%s%s)' % (base, _order_url(order))
    send_wecom(plugin, content)
    logger.info('WeiTiMDM: %s 齐套通知已发 (收件 %d 人)', ref, len(users))


def notify_shortage_created(plugin, order, created, kind):
    """自动生成下游订单时知会（可选，先静默只记日志）。"""
    for obj in created:
        logger.info('WeiTiMDM: %s %s → 自动生成 %s',
                    kind, order.reference, getattr(obj, 'reference', ''))
