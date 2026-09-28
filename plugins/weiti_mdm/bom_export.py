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


def build_bom_xlsx(part):
    """导出零件 BOM 为 xlsx 字节流。无 BOM 行时也返回（仅表头）。"""
    import openpyxl
    from part.models import BomItem

    wb = openpyxl.Workbook()
    ws = wb.active
    name = part.name or f'part{part.pk}'
    # 工作表名 ≤31 字符且不含 []:*?/\\
    title = f'{name}BOM清单'
    ws.title = title[:31].translate(str.maketrans('', '', '[]:*?/\\'))

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

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
