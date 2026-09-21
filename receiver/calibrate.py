"""calibrate 统计模式（issue #11）：识别率 / CRC 通过率 / 推荐参数。

统计口径（与 receive 主循环同源，以 pipeline 现有异常类型为准）：
- 识别率 = decode_frame 完整通过帧数 / 总帧数（FrameRejected 按原因计数）；
- CRC 通过率 = CRC 校验通过帧数 / 进入 CRC 校验帧数（geometry / sync / ver
  等前置拒绝未到达 CRC，不计入分母）；
- 统计模式不写还原文件、不建任务目录（progress / output / debug 均不触碰），
  缺帧 / 元数据缺失不影响统计完成（区别于 receive 的退出码契约）。

推荐参数（数据说话，依据 ADR-0001 后果 + pipeline._sample_grid 采样方式）：
- BIT 由角标边长（3×BIT px）测量 ÷ 3 推得，±1 像素测量误差在小 BIT 时被放大；
- 采样窗口 k = min(5, BIT)（取奇）置于单元中心，栅格原点偏差 δ px 时窗口
  不越出本单元的条件是 BIT − k ≥ 2×δ；推荐 BIT = max(实测 BIT, 满足该式的
  最小 BIT)，δ 取几何探针实测的残差最大绝对值（clean PNG 流 δ=0）；
- 推荐 PAD = max(实测 PAD, 3)（角标 3×BIT 伸入静默区，PAD ≥ 3 冻结）。
- 输出末行 JSON：BIT / PAD 与 sender.html 输入项（id=bit / id=pad）同名，
  可直接回灌发送端。
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
    """单帧计入统计：FrameRejected 即拒绝（按原因），DecodedFrame 即通过。"""
    stats.frames += 1
    if isinstance(outcome, FrameRejected):
        stats.rejected[outcome.reason] += 1
        if outcome.reason == "crc":
            stats.crc_reached += 1
        return
    stats.decoded += 1
    stats.crc_reached += 1
    stats.crc_passed += 1


def measure_stream_geometry(samples: Counter, decoded: list[DecodedFrame]) -> tuple[int, int]:
    """实测几何：探针样本多数值优先（整帧被拒也能测得），无样本时退回
    成功解码帧的帧头值（已与画面自举交叉校验一致）。"""
    if samples:
        return samples.most_common(1)[0][0]
    bits = Counter(f.header.bit for f in decoded)
    pads = Counter(f.header.pad for f in decoded)
    if not bits:
        raise ValueError("无任何几何测量样本")
    return bits.most_common(1)[0][0], pads.most_common(1)[0][0]


def _fold(value: int, modulus: int) -> int:
    """余数折叠到带符号半周期：modulus=4 时 3 → -1（-1px 偏差不会被读成 +3px）。"""
    r = value % modulus
    return r - modulus if r * 2 > modulus else r


@dataclass
class GeometrySample:
    """几何探针单帧样本（原始测量值，未经冻结校验）。"""

    bit: int
    pad: int
    residual: int  # 栅格原点 / 角标边长相对 BIT 栅格的最大折算残差（px）


def probe_frame(img: np.ndarray) -> GeometrySample | None:
    """几何探针（#11）：绕过冻结校验直接测量，统计 ±1px 级测量误差（ADR-0001）。

    ±1px 偏差在 decode_frame 中表现为整帧拒绝，统计上只剩"拒了"而分不清
    误差大小；探针取 measure_geometry 的原始测量值折算残差。测量不可能
    （无角标候选等）返回 None。
    """
    if img.ndim != 2:
        return None
    _, bw = cv2.threshold(img, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    try:
        geo, rect = measure_geometry(bw)
    except FrameRejected:
        return None
    residuals = [_fold(geo.side, 3)]
    if geo.side % 3 == 0:
        # 边长测量可信（3 的整倍数）时才统计栅格原点残差，否则 BIT 不可信
        x0, y0, _, _ = rect
        residuals += [_fold(x0, geo.bit), _fold(y0, geo.bit)]
    return GeometrySample(bit=int(geo.bit), pad=int(geo.pad),
                          residual=int(max(abs(r) for r in residuals)))


def min_safe_bit(delta_px: int) -> int:
    """最小安全 BIT：采样窗口 k = min(5, BIT)（取奇）不越出本单元的条件
    BIT − k ≥ 2×δ 的最小解（上限 15 = 帧头 GEO 4 bit）。

    δ=1（±1px 测量误差，ADR-0001 后果）→ BIT=7；clean 流 δ=0 → BIT=1
    （即无误差证据时不虚报，维持实测值）。
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
    measured_bit, measured_pad = measure_stream_geometry(
        Counter((s.bit, s.pad) for s in samples), decoded)
    residual_px = max((s.residual for s in samples), default=0)
    return {
        "frames": stats.frames,
        "decoded": stats.decoded,
        "decodeRate": decode_rate,
        "crcReached": stats.crc_reached,
        "crcPassed": stats.crc_passed,
        "crcRate": crc_rate,
        "rejected": dict(stats.rejected),
        "probed": len(samples),
        "measuredBit": measured_bit,
        "measuredPad": measured_pad,
        "residualPx": residual_px,
        "BIT": max(measured_bit, min_safe_bit(residual_px)),
        "PAD": max(measured_pad, 3),
    }


def format_report(report: dict) -> str:
    """人读摘要（机器可读 JSON 由 CLI 另起末行输出）。"""
    return "\n".join([
        f"帧数：{report['frames']}；识别 {report['decoded']}；"
        f"拒绝分布：{report['rejected'] or '无'}",
        f"识别率：{report['decodeRate']:.1%}（{report['decoded']}/{report['frames']}）",
        f"CRC 通过率：{report['crcRate']:.1%}"
        f"（{report['crcPassed']}/{report['crcReached']}，进入 CRC 校验帧数）",
        f"实测几何：BIT={report['measuredBit']} PAD={report['measuredPad']}；"
        f"栅格原点最大残差 {report['residualPx']} px",
        f"推荐参数：BIT={report['BIT']} PAD={report['PAD']}",
    ])


def run_calibration(frames) -> dict:
    """帧迭代器 → 统计报告 dict（含推荐参数，字段见 build_report）。"""
    stats = CalibrationStats()
    decoded: list[DecodedFrame] = []
    samples: list[GeometrySample] = []
    for _name, img in frames:
        try:
            outcome = decode_frame(img)
        except FrameRejected as e:
            outcome = e
        tally_frame(stats, outcome)
        if isinstance(outcome, DecodedFrame):
            decoded.append(outcome)
        sample = probe_frame(img)
        if sample is not None:
            samples.append(sample)
    return build_report(stats, samples, decoded)
