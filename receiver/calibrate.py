"""calibrate 统计模式（issue #11）：识别率 / CRC 通过率 / 推荐参数。

统计口径（与 receive 主循环同源，以 pipeline 现有异常类型为准）：
- 识别率 = decode_frame 完整通过帧数 / 总帧数（FrameRejected 按原因计数）；
- CRC 通过率 = CRC 校验通过帧数 / 进入 CRC 校验帧数（geometry / sync / ver
  等前置拒绝未到达 CRC，不计入分母）；
- 统计模式不写还原文件、不建任务目录（progress / output / debug 均不触碰），
  缺帧 / 元数据缺失不影响统计完成（区别于 receive 的退出码契约）。

推荐参数（数据说话，依据 ADR-0001 后果 + pipeline._sample_grid 采样方式）：
- BIT 由角标边长（3×BIT px）测量 ÷ 3 推得；采样窗口 k = min(5, BIT)（取奇）
  置于单元中心，网格定位偏差 δ px 时窗口不越出本单元的条件是 BIT − k ≥ 2δ；
  推荐 BIT = max(实测 BIT, 满足该式的最小 BIT)（采样鲁棒性下限）。
- δ 取几何探针实测的残差最大绝对值。裁切容忍（issue #22）后网格由角标
  逐帧锚定、随画面一同平移，栅格原点残差不可用（合法裁切偏移与测量误差
  不可区分），角标候选又要求边长为 3 的整倍数——PNG 精确流 δ 恒为 0，
  推荐即实测值；δ 通道保留给降质源（camera #13）的探针扩展。
- PAD 以帧头声明为准（裁切下几何不可测，#22），推荐 = 实测帧头多数值
  （钳到冻结下限 3）。
- 输出末行 JSON：BIT / PAD 与 sender.html 输入项（id=bit / id=pad）同名，
  可直接回灌发送端。FPS 不在推荐之列：画面统计不出帧率依据，不虚报。
"""

import cv2
import numpy as np
from collections import Counter
from dataclasses import dataclass, field

from receiver.pipeline import (
    DecodedFrame,
    FrameRejected,
    decode_frame,
    measure_geometry,
)


@dataclass
class CalibrationStats:
    """逐帧统计累计（口径见模块 docstring）。"""

    frames: int = 0
    decoded: int = 0
    rejected: Counter = field(default_factory=Counter)  # FrameRejected.reason → 帧数
    crc_reached: int = 0   # 通过前置冻结校验、到达 CRC 的帧数
    crc_passed: int = 0    # 其中 CRC 校验通过的帧数（= 完整 decode 通过）


def tally_frame(stats: CalibrationStats, outcome) -> None:
    """单帧计入统计：FrameRejected 即拒绝（按原因），DecodedFrame 即通过。

    CRC 口径按 pipeline 校验顺序：CRC 在几何/帧头冻结校验之后、元数据校验
    （reason='metadata'）之前——后者 CRC 已通过，计入分子分母两者。"""
    stats.frames += 1
    if isinstance(outcome, FrameRejected):
        stats.rejected[outcome.reason] += 1
        if outcome.reason == "crc":
            stats.crc_reached += 1
        elif outcome.reason == "metadata":  # CRC 已过，随后被元数据校验拒绝
            stats.crc_reached += 1
            stats.crc_passed += 1
        return
    stats.decoded += 1
    stats.crc_reached += 1
    stats.crc_passed += 1


def measure_stream_geometry(decoded: list[DecodedFrame],
                            samples: list["GeometrySample"]) -> tuple[int, int] | None:
    """实测几何：优先取成功解码帧帧头的多数值（已与画面自举交叉校验一致，
    不受探针边长截断影响）；无解码帧（整流被冻结校验拒绝等）退回探针
    样本多数值——探针绕过冻结校验，整流被拒时也能测得几何。
    一帧都测不出（垃圾画面流）返回 None。"""
    if decoded:
        bits = Counter(f.header.bit for f in decoded)
        pads = Counter(f.header.pad for f in decoded)
        return bits.most_common(1)[0][0], pads.most_common(1)[0][0]
    if samples:
        bit, pad = Counter((s.bit, s.pad) for s in samples).most_common(1)[0][0]
        return bit, pad
    return None


def _fold(value: int, modulus: int) -> int:
    """余数折叠到带符号半周期：modulus=4 时 3 → -1（-1px 偏差不会被读成 +3px）。"""
    r = value % modulus
    return r - modulus if r * 2 > modulus else r


@dataclass
class GeometrySample:
    """几何探针单帧样本（原始测量值，未经冻结校验）。"""

    bit: int
    pad: int
    residual: int  # 角标边长相对 3×BIT 的最大折算残差（px；裁切容忍后原点残差不可用，#22）


def probe_frame(img: np.ndarray) -> GeometrySample | None:
    """几何探针（#11）：绕过冻结校验直接测量，统计 ±1px 级测量误差（ADR-0001）。

    ±1px 偏差在 decode_frame 中表现为整帧拒绝，统计上只剩"拒了"而分不清
    误差大小；探针取 measure_geometry 的原始测量值折算残差。裁切容忍
    （#22）后栅格原点残差不可用（合法裁切偏移与测量误差不可区分），仅
    保留角标边长折算残差——角标候选要求边长为 3 的整倍数，PNG 精确流
    恒为 0，通道保留给降质源（camera #13）探针。测量不可能（无角标候选
    等）返回 None。
    """
    if img.ndim != 2:
        return None
    _, bw = cv2.threshold(img, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    try:
        geo, _rect = measure_geometry(bw)
    except FrameRejected:
        return None
    return GeometrySample(bit=int(geo.bit), pad=int(geo.pad),
                          residual=int(abs(_fold(geo.side, 3))))


def min_safe_bit(delta_px: int) -> int:
    """最小安全 BIT：采样窗口 k = min(5, BIT)（取奇）不越出本单元的条件
    BIT − k ≥ 2×δ 的最小解（上限 15 = 帧头 GEO 4 bit）。

    δ=1（±1px 级网格定位偏差）→ BIT=7；δ=0（无误差证据）→ BIT=1
    （不虚报，维持实测值）。裁切容忍后 PNG 精确流 δ 恒为 0（#22），
    通道保留给降质源（camera #13）。
    """
    for bit in range(1, 16):
        k = min(5, bit)
        if k % 2 == 0:
            k -= 1
        if bit - k >= 2 * delta_px:
            return bit
    return 15


def build_report(stats: CalibrationStats, samples: list[GeometrySample],
                 decoded: list[DecodedFrame]) -> dict:
    """统计结果 + 推荐参数（规则见模块 docstring）。空流返回零报告，
    由 CLI 判定"无帧可统计"（不输出推荐参数）。"""
    if stats.frames == 0:
        return {"frames": 0}
    decode_rate = stats.decoded / stats.frames if stats.frames else 0.0
    crc_rate = stats.crc_passed / stats.crc_reached if stats.crc_reached else 0.0
    geo = measure_stream_geometry(decoded, samples)
    residual_px = max((s.residual for s in samples), default=0)
    # geo None（垃圾画面流，一帧都测不出）→ BIT/PAD = 0 表示无法推荐
    measured_bit, measured_pad = geo if geo is not None else (0, 0)
    return {
        "frames": stats.frames,
        "decoded": stats.decoded,
        "decodeRate": decode_rate,
        "crcReached": stats.crc_reached,
        "crcPassed": stats.crc_passed,
        "crcRate": crc_rate,
        "rejected": dict(stats.rejected),
        "probed": len(samples),
        "measuredBit": int(measured_bit),
        "measuredPad": int(measured_pad),
        "residualPx": int(residual_px),
        "BIT": max(int(measured_bit), min_safe_bit(residual_px)) if geo is not None else 0,
        "PAD": max(int(measured_pad), 3) if geo is not None else 0,
    }


def format_report(report: dict) -> str:
    """人读摘要（机器可读 JSON 由 CLI 另起末行输出）。"""
    lines = [
        f"帧数：{report['frames']}；识别 {report['decoded']}；"
        f"拒绝分布：{report['rejected'] or '无'}",
        f"识别率：{report['decodeRate']:.1%}（{report['decoded']}/{report['frames']}）",
        f"CRC 通过率：{report['crcRate']:.1%}"
        f"（{report['crcPassed']}/{report['crcReached']}，进入 CRC 校验帧数）"
        if report["crcReached"] else "CRC 通过率：无帧进入 CRC 校验",
        f"实测几何：BIT={report['measuredBit']} PAD={report['measuredPad']}；"
        f"角标最大折算残差 {report['residualPx']} px"
        if report["measuredBit"] else
        "实测几何：无有效测量样本（画面无角标结构）",
        f"推荐参数：BIT={report['BIT']} PAD={report['PAD']}",
    ]
    if report["residualPx"] >= 1:
        # 推荐的作用边界（code-review #11）：±1px 级角标测量偏差会侵蚀采样
        # 窗口，小 BIT 时易越界导致 CRC 拒帧；BIT 只决定采样鲁棒性下限
        # （ADR-0001），画面损伤须在采集端解决
        lines.append(
            f"警告：实测 ±{report['residualPx']}px 级角标测量偏差，小 BIT 时"
            "采样窗口易越界（CRC 拒帧）；推荐 BIT 为采样鲁棒性下限（ADR-0001）"
        )
    return "\n".join(lines)


def run_calibration(frames) -> dict:
    """帧迭代器 → 统计报告 dict（含推荐参数，字段见 build_report）。

    Ctrl+C（desktop 等无限源）不丢弃已统计帧：中断时返回部分报告，
    report['interrupted'] = True。"""
    stats = CalibrationStats()
    decoded: list[DecodedFrame] = []
    samples: list[GeometrySample] = []
    interrupted = False
    try:
        for _name, img in frames:
            try:
                outcome = decode_frame(img)
            except FrameRejected as e:
                outcome = e
            tally_frame(stats, outcome)
            if isinstance(outcome, DecodedFrame):
                decoded.append(outcome)
                # 通过帧边长必为 3×BIT（冻结校验），残差记 0；探针只测被拒帧
                samples.append(GeometrySample(bit=outcome.header.bit,
                                              pad=outcome.header.pad, residual=0))
            else:
                sample = probe_frame(img)
                if sample is not None:
                    samples.append(sample)
    except KeyboardInterrupt:
        interrupted = True
    report = build_report(stats, samples, decoded)
    report["interrupted"] = interrupted
    return report
