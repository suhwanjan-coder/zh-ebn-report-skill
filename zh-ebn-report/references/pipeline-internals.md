# Pipeline 內部實作說明

本檔記錄 `zh-ebn-report` Python CLI 的實作細節（LLM 後端、guardrail、回歸驗證、audit artifact），供維護 pipeline 時查閱；協助護理師寫報告時不需要讀這份。

**LLM 後端**：pipeline 預設走 Claude Code CLI（你的 Claude 訂閱），不再強制 `ANTHROPIC_API_KEY`。透過 `LLM_BACKEND` 環境變數切換：

- `LLM_BACKEND=claude_code`（預設）— 以 subprocess 呼叫 `claude -p`，走訂閱。需 PATH 上有 `claude` CLI
- `LLM_BACKEND=anthropic` — 直接走 Anthropic SDK；需 `ANTHROPIC_API_KEY`（或 `LLM_API_KEY`），適合 CI 或沒訂閱的環境
- `LLM_BACKEND=auto` — 偵測有無 `claude` CLI 再選

實作見 `clients/llm.py`（Protocol + factory）、`clients/claude_code_cli.py`（CLI 後端）、`clients/anthropic.py`（SDK 後端）。兩個後端對外介面一致，`complete()` / `complete_json()` 可互換。並發：多個 LLM call 起多個 `claude` subprocess，由 `max_parallel_casp` 等 config 限流。

**Guardrail 架構**：pipeline 不再只靠 LLM 自律。每一個 LLM 寫在 prompt 的「硬性規定」只要是機械可驗證的，都有對應 Python guardrail 在 orchestrator 裡覆寫 LLM 自評結果，或在 `compliance.check_sections` 裡擋下來：

- `pipeline/evidence_guard.py` — Oxford Level 不可超過 study_design 的 OCEBM 2011 天花板（MA-of-cohort 絕不可 Level I）
- `pipeline/synthesis_guard.py` — `overall_evidence_strength` 從 CASP levels + contradictions 機械推導
- `pipeline/voice_scan.py` — regex 掃禁用詞並重算 `pass_threshold_met`
- `pipeline/apa_guard.py` — `apa_pass` 依 DOI 驗證 + citation 存在性 + LLM format_issues 推導
- `pipeline/compliance.py` — 句型、字數、引文、匿名、privacy、絕對用語、citation 捏造防線

**回歸驗證工具**：每次新增或修改 guardrail 後執行：

```
python scripts/retro_validate.py
```

把 `output/<run-id>/state.json` 的歷史資料全部載入，用當前 guardrail 套一遍，報告「如果當時就有這些 guardrail，會抓到什麼」。用於 (a) 確認 guardrail 捕捉到真實違規、(b) 避免回歸。加 `--json` 輸出給 CI；加 `--strict` 讓任何 guardrail 發現都退出碼 1。

**Audit Artifact Store**：每個 run 在 `output/<run-id>/artifacts/` 留一整組中間產物供日後審查：

```
artifacts/
  _index.jsonl                  # append-only 所有 artifact 的紀錄（timestamp, category, path, meta）
  blobs/<sha256>.txt            # 內容定址儲存（system prompt 60KB × 30 次只存 1 份）
  llm/<ISO>_<caller>_<tier>_<id>.json  # 每次 LLM 呼叫的完整記錄
  guardrails/<name>/<ISO>_{before,after,summary}.json  # 每道 guardrail 的 before/after/summary
```

LLM record 含：呼叫者函式名（自動偵測 stack）、tier、model、backend（anthropic / claude_code）、duration_ms、system prompt hashes、user msg hash、response raw hash、response_parsed。Guardrail 紀錄讓「LLM 原始判讀 vs Python 覆寫後」兩個版本都保留。

實作見 `pipeline/audit.py`（`ArtifactStore`）與 `clients/audited.py`（`AuditedLLMClient`）。Orchestrator 在每個 phase 入口呼叫 `_bind_to_run(state)` 建立對應 run 的 store，LLM 與 guardrail 呼叫透明落檔。

**台灣護理強化（v0.8+）**：針對台灣護理實務三個核心需求的專屬支援。

1. **華藝 Airiti 匯入**：除 RIS 外新增 CSV 支援（Airiti 書目匯出預設格式）。
   - `zh-ebn-report search <run-id> --airiti-csv <file.csv>` — 讀取標題/作者/年份/期刊/DOI/摘要/類型
   - 自動偵測「學位論文」並固定 `StudyDesign.OTHER` + `OxfordLevel.IV`（學位論文未經同儕審查，不得提升等級）
   - 中文作者姓名（`張小明` 等純 CJK 字串）自動以前 2 字做 citekey surname，解決原先 `split()` 誤切問題
   - 匯出步驟：到 Airiti Library → 勾選文獻 → 書目管理 → 選 CSV 匯出 → 下載
   - 實作見 `clients/manual_import.py:_airiti_csv_to_records`

2. **Gordon 11 項功能性健康型態**：TWNA 個案報告／護理專案 `護理評估` 節的標準骨架。
   - 參考：`references/gordon-11-patterns.md`（11 項定義 + 主觀/客觀資料模板 + NANDA 診斷對照）
   - 撰寫 role：`prompts/section_writer_護理評估.md`，要求 LLM 逐項產出 S/O + 評估發現
   - Compliance：`_check_gordon_11_coverage` 掃 `護理評估` 節是否涵蓋 ≥ 9/11 項關鍵詞（彈性門檻，第 9 性-生殖可在急性/幼童情境略過）
   - 只對 `twna_case` / `twna_project` 類型觸發，reading/case 類型不強制

3. **AI 關鍵字調整（keyword tuner）**：PubMed 初次檢索若落在 100–1000「甜蜜區」之外，可選啟用一輪 LLM 調整。
   - 啟用：`ENABLE_KEYWORD_TUNER=1`（預設關）
   - 觸發條件：初輪 hits < 50（太窄）或 > 5000（太寬）
   - 調整員：`prompts/keyword_tuner.md`（Haiku 等級，只改 Boolean 字串）
   - 決策：`pipeline/keyword_tuner.pick_better` 挑離甜蜜區較近的那一版；`SearchHistoryRow.note` 兩輪都記錄，審計可見
   - 上限 1 輪；第二輪仍超出就接受現況、不再呼叫 tuner（控成本）
