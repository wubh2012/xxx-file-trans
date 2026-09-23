"""区域框选（pick，issue #22）：desktop 源 `--region pick` 的交互定区。

启动时经 mss 抓全虚拟屏（monitors[0]，支持负坐标原点）快照，tkinter
全屏置顶无装饰覆盖窗 1:1 显示快照，橡皮筋拖框 → Enter 确认 / 再次拖拽
替换 / Esc 取消（Spec Implementation Decisions）。返回的 region dict 与
mss `--region L,T,W,H` 同构直通。

物理像素口径：CLI 进程导入 receiver.sources.desktop 时已声明 per-monitor
DPI awareness（先于本模块任何 Tk() 创建），覆盖窗坐标即物理像素，
`--region` 逐像素对齐的前提。

公开接口：
- normalize_rect / region_from_drag：坐标换算纯函数（无头测试，Spec 缝 3）；
- pick_region：tkinter 薄壳，真实屏幕交互人工验收（同项目惯例）。
"""


def normalize_rect(x0: int, y0: int, x1: int, y1: int) -> tuple[int, int, int, int]:
    """拖框对角坐标 → (left, top, width, height)：方向无关、宽高下限 1。

    橡皮筋可往任意方向拖；原地单击（零尺寸）钳到 1×1，不产生 0 宽高。
    """
    left, top = min(x0, x1), min(y0, y1)
    return left, top, max(1, abs(x1 - x0)), max(1, abs(y1 - y0))


def region_from_drag(x0: int, y0: int, x1: int, y1: int,
                     vs_left: int, vs_top: int) -> dict:
    """覆盖窗内拖框坐标 → mss 捕获区域 dict（物理像素，含虚拟屏原点偏移）。

    覆盖窗锚定虚拟屏并集原点 (vs_left, vs_top)（多显示器可为负），
    窗内坐标 + 原点 = 物理屏幕坐标，与 mss region 同一坐标系直通。
    """
    left, top, width, height = normalize_rect(x0, y0, x1, y1)
    return {"left": vs_left + left, "top": vs_top + top,
            "width": width, "height": height}


def pick_region() -> dict | None:
    """冻屏框选（tkinter 薄壳，人工验收）：返回 mss region dict 或 None（取消）。

    交互：拖框（橡皮筋）→ Enter 确认 / 再次拖拽替换 / Esc 取消；
    零位移的单击不改写已框区域（防误触）。虚拟屏快照以 PPM 内嵌
    PhotoImage 1:1 显示（零新依赖，不引 PIL）。
    """
    import tkinter as tk

    import cv2
    import numpy as np

    import mss  # 与 desktop 源同款延迟导入

    with mss.mss() as sct:
        mon = sct.monitors[0]  # 全部显示器的虚拟屏并集（支持负坐标原点）
        bgra = np.asarray(sct.grab(mon))
    vs_left, vs_top = mon["left"], mon["top"]

    h, w = bgra.shape[:2]
    ppm = b"P6\n%d %d\n255\n" % (w, h) + cv2.cvtColor(bgra, cv2.COLOR_BGRA2RGB).tobytes()

    root = tk.Tk()
    root.title("选择采集区域")
    # 无装饰置顶覆盖窗锚定虚拟屏并集：-fullscreen 只覆盖当前显示器，
    # geometry 直排并集尺寸 + 负原点才能铺满多屏（坐标即物理像素）
    root.overrideredirect(True)
    root.geometry(f"{w}x{h}+{vs_left}+{vs_top}")
    root.attributes("-topmost", True)
    root.configure(cursor="crosshair")

    canvas = tk.Canvas(root, highlightthickness=0, cursor="crosshair")
    canvas.pack(fill="both", expand=True)
    # master=canvas 必须显式指定：本模块可能被 GUI 进程调用（receiver_gui，
    # issue #26），彼时默认根已是主窗口的另一个 Tk 解释器，快照 PhotoImage
    # 不指定 master 会挂错解释器，create_image 抛 TclError → 覆盖窗白屏
    snapshot = tk.PhotoImage(master=canvas, data=ppm)  # 1:1 显示，无缩放（引用须保活）
    canvas.create_image(0, 0, image=snapshot, anchor="nw")

    drag = {"start": None, "rect": None, "moved": False, "region": None}
    hint = {"id": None}

    def show_hint(text):
        """提示行：拖拽中让位视线，松手即恢复——「按回车确认」的提示不可缺席。"""
        if hint["id"] is not None:
            canvas.delete(hint["id"])
        hint["id"] = canvas.create_text(12, 10, anchor="nw", fill="yellow",
                                        font=("system-ui", 14), text=text)

    show_hint("拖框选择采集区域（允许留边）→ Enter 确认 / Esc 取消；重新拖拽替换")

    def on_press(event):
        drag["start"] = (event.x, event.y)
        drag["moved"] = False
        if hint["id"] is not None:  # 拖拽中让位，框选视线不被提示遮挡
            canvas.delete(hint["id"])
            hint["id"] = None
        if drag["rect"] is not None:  # 再次拖拽替换：旧橡皮筋一并清掉
            canvas.delete(drag["rect"])
            drag["rect"] = None

    def on_drag(event):
        drag["moved"] = True
        if drag["rect"] is None:
            drag["rect"] = canvas.create_rectangle(*drag["start"], event.x, event.y,
                                                   outline="red", width=2)
        else:
            canvas.coords(drag["rect"], *drag["start"], event.x, event.y)

    def on_release(_event):
        # 零位移单击不改写（防误触清掉已框区域）；真实拖拽即替换
        if drag["moved"]:
            drag["region"] = region_from_drag(*drag["start"], _event.x, _event.y,
                                              vs_left, vs_top)
        # 已框选 → 督促回车确认；未框选（零位移单击）→ 完整指引
        show_hint("Enter 确认 / Esc 取消；重新拖拽替换" if drag["region"] is not None
                  else "拖框选择采集区域（允许留边）→ Enter 确认 / Esc 取消")

    def confirm(_event=None):
        if drag["region"] is not None:
            root.destroy()

    def cancel(_event=None):
        drag["region"] = None
        root.destroy()

    canvas.bind("<ButtonPress-1>", on_press)
    canvas.bind("<B1-Motion>", on_drag)
    canvas.bind("<ButtonRelease-1>", on_release)
    root.bind("<Return>", confirm)
    root.bind("<Escape>", cancel)
    root.protocol("WM_DELETE_WINDOW", cancel)  # 无装饰几乎不可达，保险
    # wait_window 而非 mainloop（issue #26）：进程内只允许一个 mainloop，
    # GUI 宿主嵌套第二个 mainloop 后，确认销毁即卡死外层事件循环（界面
    # 假死）；wait_window（tkwait window）自带局部事件处理，CLI 无 mainloop
    # 与 GUI 嵌套两种宿主均正确
    root.wait_window(root)
    return drag["region"]
