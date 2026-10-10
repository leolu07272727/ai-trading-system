# V2 年終檢討手冊（V2_REVIEW_PLAYBOOK.md）

> 2026-07-07 由 Fable 5 於交棒前寫。目的：讓 12/31 的檢討不依賴執行者的模型等級——
> 每一題都給「用什麼數據、怎麼判、結論只能寫成什麼形狀」。
> 執行者：任何接手模型＋Leo。原則：**先取數據後下結論，敘事不得替代數字。**

## 何時觸發
- `theses.yaml` 的 `review_by: 2026-12-31` 到期；或
- falsifier 觸發全清倉（提前檢討）；或 Leo 隨時要求。

## 資料位置（全在 VPS ~/trading-bot/thesis_monitor/，正本）
| 數據 | 檔案 |
|---|---|
| 每日帳本 vs SPY | `portfolio_history.jsonl`（equity / spy_benchmark） |
| 機器成交＋理由 | `trades.jsonl`（machine_reason 欄） |
| Leo 的 ex-ante 決策 | `decisions.jsonl`（與機器帳本比對＝Phase 3） |
| evidence 全史（狀態/擁擠度/距高） | `evidence/ai-memory-2026.jsonl`（每日一行） |
| 現貨/capex 判讀軌跡 | `cycle_data.json` 的 notes（含歷次更新依據） |

## 四個檢討題（judgment 已做完，照做即可）

### 1. 擁擠悖論（最重要）
**問題**：進場時自家 evidence 已標「共識 strong_buy＝擁擠」，crowding 該不該當進場否決條件？
**判法**：掃全年 evidence jsonl，把每個「機器進場日」分成兩組——進場當日 `rec_mean ≤1.5`（擁擠）vs `>1.5`；比較兩組進場後 30/60/90 天報酬。擁擠組系統性更差 → 提案「EARLY 進場加 not-crowded 條件」（改規則需 Leo 批准，theses.yaml 與 executor.py 兩邊一起改）。樣本不足（<5 次進場）→ 寫「無法判定」，不硬答。

### 2. trim 底倉
**已量化**（見 auto-memory `project_memory_thesis.md`）：底倉 25%/50% slice ≈ +3~12pp、DD 幾乎不變＝近乎免費。
**判法**：查全年 trades.jsonl 有無「連續 trim 使部位 <10% slice」的實例；有 → 直接提案 executor.py 加 floor（值由 Leo 挑，不要在歷史樣本上掃參數選「最佳」）。

### 3. 單一 thesis 的統計力
**紅線**：一個 thesis 的結果是 n=1。結論**只能寫成**「ai-memory-2026 這一注贏/輸 SPY X%」，**不得寫成**「Leo 的 thesis 流程有/無 skill」。要 skill 級結論需 ≥3 個 thesis 完整週期（proposal 原話：3 完整循環 2-4 年）。檢討的正確產出之一是：下一個 thesis 是什麼？

### 4. 要不要價格停損
**判法**：對照全年——若曾發生「回撤已 >20% 而 falsifier 仍全綠」，說明基本面斷路器對急跌太慢，投真錢前必須補價格護欄；若 falsifier 總在大跌前/中觸發，維持「論點停損」設計。**這題在虛擬帳本階段不改，只記錄；它是真錢門檻談判的必答題。**

## 檢討時的紅線
1. 不許在同一段歷史上掃參數挑最佳（V1 的過擬合教訓，SPEC §18）。
2. 不許因 paper 賺錢提議投真錢——真錢門檻寫死在 BOT_V2_PROPOSAL.md：3 完整循環＋6 個月 ≥2 循環不輸 SPY。
3. 帳本 vs SPY 同期是唯一計分板；「如果當初」不是數據。
4. 檢討報告每個數字註明來源檔案；給不出來源的主張標「未驗證」。

## 已知邊界（別重做）
- 2026-07-07 價格腿回測：現行規則 +22.8%（maxDD -5.1%）～永不 trim +114.9%（-17.9%），真值取決於 MID 判準；成本拆解＝分段進場 ~40pp、trim 全開 ~100pp。細節在 auto-memory，**不必重跑**——狀態機歷史（EPS 估計修訂時序）免費數據拿不到，重跑只會得到同樣的邊界。
