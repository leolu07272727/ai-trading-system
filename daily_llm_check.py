#!/usr/bin/env python3
"""Daily LLM health interpretation → Telegram.

Deterministically gathers today's pipeline activity (reusing eod_summary's
parsers), asks Claude (anthropic SDK, same key as the AI judge) for a short
plain-language read on whether the bot looks healthy vs the experiment-mode
expectation (0-3 entries/day, quiet zero-days normal). Best-effort: any
failure degrades to sending the raw deterministic context, never crashes.

Implementation note: uses the anthropic SDK directly rather than the claude
CLI + --dangerously-skip-permissions, so there is no autonomous-shell risk on
the VPS and no dependency on the CLI's (expiring) OAuth login.
"""
from __future__ import annotations

import os

from dotenv import load_dotenv

from eod_summary import (
    _today,
    summarize_funnel,
    summarize_fills,
    summarize_overlay,
    summarize_positions,
)
from src.notify import notify

MODEL = "claude-sonnet-4-6"
# First trading day EXPERIMENT MODE is live (relaxed gates for sample
# collection: gap≥2%, I2 skipped → I2R "price above open", same-time RVOL≥1.5).
# Data before this ran the strict rules and near-never produced entries.
EXPERIMENT_MODE_DATE = "2026-07-02"
SYSTEM = (
    "你是一個量化交易 bot 的每日營運健檢員。根據提供的當日活動數據,用繁體中文寫 "
    "2-4 句精簡解讀:今天 bot 運作是否正常、有無異常值得注意。判斷基準:實驗模式下"
    "預期每日 0-3 筆進場訊號,安靜日掛零屬正常(市場沒有夠強的 gap);要留意的異常是"
    "『評估數為 0(系統沒在跑)』『大量 daily_data_unavailable(資料源故障)』"
    "『AI判官全部 fallback(API key 問題)』。只講重點與判斷,不要客套、不要逐項"
    "複述數字。若一切正常就明說正常。"
)


def build_context() -> str:
    today = _today()
    if today >= EXPERIMENT_MODE_DATE:
        mode_note = (
            "實驗模式(收樣本)已生效:gap≥2%、I2 已跳過(改 I2R 價>今開)、RVOL 同時段≥1.5;"
            "預期 0-3 筆進場/日,掛零=安靜日屬正常。"
        )
    else:
        mode_note = (
            f"注意:實驗模式 {EXPERIMENT_MODE_DATE} 才上線,今日數據為舊嚴格規則"
            "(幾乎不產生進場,0 訊號屬預期)。"
        )
    return "\n".join(
        [
            f"日期(ET): {today}",
            mode_note,
            summarize_funnel(today),
            summarize_overlay(today),
            summarize_fills(today),
            summarize_positions(),
        ]
    )


def interpret(context: str) -> str:
    """Ask Claude for a short read. Returns '' on any failure."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return ""
    try:
        from anthropic import Anthropic

        client = Anthropic(timeout=60)
        resp = client.messages.create(
            model=MODEL,
            max_tokens=400,
            system=SYSTEM,
            messages=[
                {
                    "role": "user",
                    "content": "以下是今天的交易 bot 活動數據,請解讀:\n\n" + context,
                }
            ],
        )
        return "".join(
            b.text for b in resp.content if getattr(b, "type", None) == "text"
        ).strip()
    except Exception as exc:  # noqa: BLE001
        print(f"[daily_llm_check] LLM call failed: {exc}")
        return ""


def main() -> int:
    load_dotenv()
    context = build_context()
    read = interpret(context)
    if read:
        body = f"{read}\n\n— 依據 —\n{context}"
    else:
        body = f"(LLM 解讀不可用,以下為原始數據)\n{context}"
    notify("🧠 AI 每日健檢", body)
    print(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
