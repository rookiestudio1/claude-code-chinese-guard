# 請用繁體中文

Claude Code 的回應變成英文、日文或簡體中文時，要求改寫成繁體中文（台灣用詞）的 Stop hook。

改寫自 [minorun365/claude-code-japanese-guard](https://github.com/minorun365/claude-code-japanese-guard)，把對象從日文換成繁體中文，並補上 Windows 相容性。

## 運作方式

每回合結束時，計算這一回合 Claude 寫給使用者的本文，符合下列任一條件就不讓回合結束，要求改寫：

| 類別 | 判定條件（預設值） |
|---|---|
| 英文 | 英文字母至少 25 個，且超過中日文字元數的 3 倍 |
| 日文 | 假名至少 10 個，且至少是漢字數的 0.2 倍 |
| 簡體字 | 只在簡體中文使用的字至少 3 個，且佔漢字的 5% 以上 |

每回合最多退回一次，改寫後如果仍不合格，也會就此結束，不會無限迴圈。退回時只要求改寫不合格的段落，已經寫好的繁中段落不會重貼。

檢查範圍：

- 最後一次工具呼叫之後的最終回答
- 工具呼叫之間的進度報告中，多行的或很長的段落（工具呼叫前的一行前言不檢查）
- 桌面版 `wrap_up` 工具顯示的「下一步」框（recap、ask、按鈕文字）

**工具呼叫的參數不檢查**：指令、程式碼、檔案內容、交給其他工具的文字（例如要求用日文說話的語音或角色台詞）都維持原本的語言。退回訊息也會明確告訴 Claude 不要改寫工具參數的語言。`wrap_up` 是唯一的例外，因為它的內容就是給使用者看的回覆，不想檢查的話可以設 `CHINESE_GUARD_CHECK_WRAP_UP=0`。

程式碼區塊（包含沒有閉合的）、行內程式碼、URL、email、Markdown 連結都不計入，所以用程式碼區塊展示英文指令或簡體原文不會被退回。回答只有 JSON 時也不檢查。

## 安裝

需要 Python 3.7 以上，不需要額外套件。

1. 下載 hook 到 `~/.claude/hooks/`。

   macOS／Linux／Git Bash：

   ```bash
   mkdir -p ~/.claude/hooks
   curl -fsSL https://raw.githubusercontent.com/rookiestudio1/claude-code-chinese-guard/main/hooks/chinese-guard.py -o ~/.claude/hooks/chinese-guard.py
   ```

   Windows PowerShell（要寫 `curl.exe`，只寫 `curl` 會變成 `Invoke-WebRequest` 的別名）：

   ```powershell
   New-Item -ItemType Directory -Force "$HOME\.claude\hooks" | Out-Null
   curl.exe -fsSL https://raw.githubusercontent.com/rookiestudio1/claude-code-chinese-guard/main/hooks/chinese-guard.py -o "$HOME\.claude\hooks\chinese-guard.py"
   ```

   hook 每一回合都會以你的權限執行，建議下載後先讀過內容。想固定版本時，把網址中的 `main` 換成某個 commit 的 SHA。

2. 在 `~/.claude/settings.json` 的 `hooks.Stop` 與 `hooks.PreToolUse` 陣列中各加入一筆，既有設定保留不動。

   Windows（路徑請換成自己的使用者目錄）：

   ```json
   {
     "hooks": {
       "Stop": [
         { "hooks": [ { "type": "command", "command": "python C:/Users/<使用者>/.claude/hooks/chinese-guard.py" } ] }
       ],
       "PreToolUse": [
         { "matcher": "mcp__.*__wrap_up", "hooks": [ { "type": "command", "command": "python C:/Users/<使用者>/.claude/hooks/chinese-guard.py" } ] }
       ]
     }
   }
   ```

   macOS／Linux：把 `command` 換成 `python3 ~/.claude/hooks/chinese-guard.py`。

   `matcher` 只寫 `wrap_up` 會被當成完全比對而對不到實際的工具名稱，請照上面的寫法。

3. 確認 hook 可以正常執行（沒有輸出、結束碼為 0 就是正常）：

   ```bash
   echo '{}' | python ~/.claude/hooks/chinese-guard.py && echo OK
   ```

4. 新開的 session 才會生效。

### 和原專案的差異

- 退回訊息改成要求繁體中文（台灣用詞），也要求工具參數維持原本的語言
- 新增日文與簡體字偵測
- stdin 一律以 UTF-8 解碼，輸出的 JSON 一律跳脫成 ASCII，繁中 Windows（cp950）上也不會出現編碼錯誤
- 不靠 shebang 與執行權限，直接用 `python` 執行
- 等待最終回答寫入時，只增量讀取 transcript 新增的部分，長 session 也不會變慢
- 沒有閉合的程式碼區塊也會忽略

## 環境變數

| 變數 | 預設 | 意義 |
|---|---|---|
| `CHINESE_GUARD_MIN_LATIN` | `25` | 判定英文所需的最少英文字母數 |
| `CHINESE_GUARD_MIN_LATIN_MID` | `60` | 多行進度報告判定英文所需的最少英文字母數 |
| `CHINESE_GUARD_MIN_LATIN_MID_LONG` | `250` | 單行進度報告判定英文所需的最少英文字母數 |
| `CHINESE_GUARD_MIN_CJK_MID_LONG` | `100` | 單行進度報告判定日文、簡體字所需的最少中日文字元數 |
| `CHINESE_GUARD_RATIO` | `3` | 英文字母數超過中日文字元數的幾倍，就視為英文 |
| `CHINESE_GUARD_MIN_KANA` | `10` | 判定日文所需的最少假名數 |
| `CHINESE_GUARD_KANA_RATIO` | `0.2` | 假名數至少是漢字數的幾倍，才視為日文 |
| `CHINESE_GUARD_MIN_SIMPLIFIED` | `3` | 判定簡體字所需的最少簡體字數 |
| `CHINESE_GUARD_SIMPLIFIED_RATIO` | `0.05` | 簡體字佔漢字的比例達多少，才視為簡體中文 |
| `CHINESE_GUARD_CHECK_JAPANESE` | `1` | 設 `0` 不檢查日文 |
| `CHINESE_GUARD_CHECK_SIMPLIFIED` | `1` | 設 `0` 不檢查簡體字 |
| `CHINESE_GUARD_CHECK_WRAP_UP` | `1` | 設 `0` 不檢查「下一步」框 |
| `CHINESE_GUARD_WAIT` | `3` | 等待最終回答寫入 transcript 的秒數 |
| `CHINESE_GUARD_DISABLE` | 無 | 設 `1` 停用（給 `claude -p` 等腳本用） |

## 用過去的 session 測試

傳入 transcript，會列出最後一回合不合格的段落：

```bash
python ~/.claude/hooks/chinese-guard.py --check ~/.claude/projects/<專案>/<session ID>.jsonl
```

## 測試

```bash
python tests/test_chinese_guard.py
```

預設以「核心數減 1」個執行緒同時跑；`--jobs N` 指定數量，`--shard i/n` 只跑第 i 份（從 0 起算）。hook 會以 `PYTHONUTF8=0`、UTF-8 位元組輸入執行，模擬繁中 Windows 的環境。

## 授權

Apache License 2.0，詳見 `LICENSE` 與 `NOTICE`。
