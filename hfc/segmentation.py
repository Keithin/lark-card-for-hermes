"""超限分段（card.over_limit: segmented）：把一篇装不下的正文切成「主卡 + 编号续卡」。

设计约束（对齐上游 #511/#513 并适配本插件）：
- 切分必须**无损**：按 markdown 块边界切，不切断表格/代码围栏；单块都装不下就直接认输；
- 每张卡必须**逐个验证**装得下（`fits` 回调由调用方渲染 + `inspect_card_limits` 给出）；
- 张数有硬上限（默认 5）：本模式的定位是「一张卡装不下、发文档又太重」的中间段，不是正文容器；
- 任何一步不可行就返回 None，调用方回退既有的原生文本投递 —— 重复优于缺失，绝不静默截断。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

from .text import scan_markdown_blocks

MAX_SEGMENTS = 5


@dataclass(frozen=True)
class SegmentPlan:
    chunks: tuple[str, ...]
    reason: str = ""

    @property
    def count(self) -> int:
        return len(self.chunks)


def _blocks(text: str) -> list[str]:
    blocks = [block.text for block in scan_markdown_blocks(text)]
    return [block for block in blocks if block.strip()] or [text]


def plan_segments(
    text: str,
    *,
    fits: Callable[[str], bool],
    max_segments: int = MAX_SEGMENTS,
) -> "SegmentPlan | None":
    """按块贪心装箱；任一单块装不下、或张数超上限 → None（调用方回退原生文本）。"""
    body = str(text or "")
    if not body.strip():
        return None
    if fits(body):
        return SegmentPlan(chunks=(body,), reason="fits")

    chunks: list[str] = []
    current = ""
    for block in _blocks(body):
        candidate = f"{current}\n\n{block}" if current else block
        if fits(candidate):
            current = candidate
            continue
        if not current:
            # 单个块就装不下：不硬切表格/代码，直接认输交给原生文本。
            return None
        chunks.append(current)
        if len(chunks) >= max_segments:
            return None
        if not fits(block):
            return None
        current = block
    if current:
        chunks.append(current)
    if len(chunks) > max_segments or not chunks:
        return None
    return SegmentPlan(chunks=tuple(chunks), reason="segmented")


def segment_title(base: str, index: int, total: int) -> str:
    """`Hermes Agent（2/3）`：编号放标题，续卡一眼能对齐。"""
    title = (base or "").strip() or "Hermes Agent"
    if total <= 1:
        return title
    return f"{title}（{index + 1}/{total}）"


def segment_delivery_key(base_key: str, index: int) -> str:
    """续卡投递键必须确定，重复终态事件才不会重发。"""
    return f"{base_key or 'segment'}:segment:{index}"
