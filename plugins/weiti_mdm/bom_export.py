"""BOM 导出：把零件的 BOM 表导出为带嵌入图片的 xlsx。

与 bom_import.py 对称：列结构参照车间 BOM 表
（序号/类别/名称/IPN/图片/规格/数量/位号/备注）。
"""

import io
import logging

logger = logging.getLogger('inventree')

HEADERS = ['序号', '类别', '名称', 'IPN', '图片', '规格', '数量', '位号', '备注']
COL_WIDTHS = [6, 14, 22, 18, 32, 40, 8, 12, 20]

# 单元格图片框：宽 ~32字符≈224px，高 ~120px（行高 90pt）
IMG_BOX_W, IMG_BOX_H = 210, 110


def _fit_image(path):
    """按单元格尺寸缩放图片，返回 openpyxl Image 或 None。"""
    try:
        from openpyxl.drawing.image import Image as XLImage
        from PIL import Image as PILImage
        img = XLImage(path)
        try:
            w, h = PILImage.open(path).size
        except Exception:
            w, h = img.width, img.height
        if not w or not h:
            return None
        scale = min(IMG_BOX_W / w, IMG_BOX_H / h, 1.0)
        img.width, img.height = int(w * scale), int(h * scale)
        return img
    except Exception:
        logger.exception('WeiTiMDM.bom_export: 图片处理失败 %s', path)
        return None


def _sheet_title(name, used):
    """工作表名：≤31字符、去非法字符、重名加 _2/_3 后缀。"""
    base = f'{name}BOM'[:28].translate(str.maketrans('', '', '[]:*?/\\')) or 'BOM'
    title, n = base, 1
    while title in used:
        n += 1
        title = f'{base}_{n}'[:31]
    used.add(title)
    return title


def _fill_sheet(ws, part):
    """把一个零件的 BOM 填进工作表（含图片）。"""
    from part.models import BomItem

    ws.append(HEADERS)
    for i, w in enumerate(COL_WIDTHS):
        ws.column_dimensions[chr(ord('A') + i)].width = w
    ws.row_dimensions[1].height = 22

    items = (BomItem.objects.filter(part=part)
             .select_related('sub_part', 'sub_part__category')
             .order_by('pk'))
    for i, it in enumerate(items, start=1):
        sp = it.sub_part
        excel_row = i + 1
        ws.append([
            i,
            sp.category.name if sp.category else '',
            sp.name,
            sp.IPN or '',
            '',
            sp.description or '',
            float(it.quantity),
            it.reference or '',
            it.note or '',
        ])
        ws.row_dimensions[excel_row].height = 90
        try:
            img_path = sp.image.path if sp.image else None
        except Exception:
            img_path = None
        if img_path:
            img = _fit_image(img_path)
            if img is not None:
                ws.add_image(img, f'E{excel_row}')


def collect_assemblies(part):
    """递归收集装配体：自身 + 所有 BOM 子件中是装配体的（BFS，去重）。"""
    from part.models import BomItem, Part
    seen, queue, out = {part.pk}, [part], []
    while queue:
        cur = queue.pop(0)
        out.append(cur)
        subs = (BomItem.objects.filter(part=cur)
                .values_list('sub_part_id', flat=True))
        for sp in Part.objects.filter(pk__in=list(subs), assembly=True):
            if sp.pk not in seen:
                seen.add(sp.pk)
                queue.append(sp)
    return out


def build_bom_book(parts):
    """多个零件的 BOM 合并导出：每个零件一个工作表。"""
    import openpyxl
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    used = set()
    for part in parts:
        ws = wb.create_sheet(
            _sheet_title(part.name or f'part{part.pk}', used))
        _fill_sheet(ws, part)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_bom_xlsx(part):
    """单零件导出（兼容旧调用）。"""
    return build_bom_book([part])
