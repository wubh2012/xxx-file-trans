"""区域框选 pick 的坐标换算纯函数（issue #22，Spec 缝 3）。

只测公共接口（receiver.pick.normalize_rect / region_from_drag）：
拖框方向归一化、宽高下限、虚拟屏负原点偏移、物理像素直通。
tkinter 覆盖窗本体是薄壳，真实屏幕交互人工验收（同项目惯例），不进自动化。
"""

from receiver.pick import normalize_rect, region_from_drag


# ---------- normalize_rect：拖框对角坐标 → (left, top, width, height) ----------

def test_normalize_rect_topleft_origin():
    """左上→右下正向拖拽：原样归一。"""
    assert normalize_rect(10, 20, 110, 220) == (10, 20, 100, 200)


def test_normalize_rect_direction_agnostic():
    """任意方向拖拽（右下→左上等）都归一为 left/top 非负宽高。"""
    assert normalize_rect(110, 220, 10, 20) == (10, 20, 100, 200)
    assert normalize_rect(110, 20, 10, 220) == (10, 20, 100, 200)
    assert normalize_rect(10, 220, 110, 20) == (10, 20, 100, 200)


def test_normalize_rect_zero_drag_clamps_to_1px():
    """原地单击（零尺寸拖框）钳到 1×1，不产生 0 宽高区域。"""
    assert normalize_rect(50, 60, 50, 60) == (50, 60, 1, 1)


# ---------- region_from_drag：覆盖窗坐标 → mss region（物理像素直通） ----------

def test_region_from_drag_primary_monitor():
    """虚拟屏原点 (0,0)：窗内坐标即物理像素，与 mss region 直通。"""
    assert region_from_drag(10, 20, 110, 220, 0, 0) == {
        "left": 10, "top": 20, "width": 100, "height": 200,
    }


def test_region_from_drag_negative_virtual_origin():
    """多显示器负坐标原点（左侧副屏）：region 含虚拟屏原点偏移，
    物理像素口径不变——mss 按同一坐标系抓屏，逐像素对齐。"""
    assert region_from_drag(100, 200, 300, 400, -1920, -100) == {
        "left": -1820, "top": 100, "width": 200, "height": 200,
    }
