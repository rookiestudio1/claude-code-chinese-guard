#!/usr/bin/env python3
"""Claude Code 的 Stop hook：回應不是繁體中文時，不讓這一回合結束，要求改寫成繁體中文（台灣用詞）。

改寫自 minorun365/claude-code-japanese-guard（Apache License 2.0）。

偵測三種情況：
1. 英文為主：英文字母數超過中日文字元數的 RATIO 倍。
2. 日文：平假名、片假名佔一定比例。
3. 簡體字：出現一定比例只在簡體中文使用的字。

檢查範圍：
1. 本回合的最終回答，也就是最後一次工具呼叫之後 Claude 寫的本文。
   沒有呼叫工具的回合，全部本文都是最終回答。
2. 桌面版「下一步」框（wrap_up 工具）的 recap、ask 與按鈕文字。
   框取代最終回答的回合幾乎沒有本文，只看 1 會漏掉。
3. 工具呼叫之間、有一定份量的進度報告。多行才檢查；單行只有很長時才檢查。
   工具呼叫前的一行前言在畫面上只是小字，退回的話，繁中的最終回答之後
   會再多出一則只改寫前言的回應，反而難讀。
   進度報告也可能記錄成 thinking 區塊（thinkingDisplay 為 updates）。
wrap_up 也以 PreToolUse hook 用同一套規則擋下，在框顯示到畫面前就退回。
最終回答同時看 hook 輸入的 last_assistant_message，transcript 寫入延遲也不會漏掉。
那裡只有最後一則回應，所以 transcript 的判定也保留，兩邊都看。

工具呼叫的參數一律不檢查（指令、程式碼、交給其他工具的台詞等維持原本語言），
退回訊息也明確要求不要改動工具參數的語言。唯一的例外是 wrap_up，因為它的內容就是給使用者看的回覆；
不想檢查時設 CHINESE_GUARD_CHECK_WRAP_UP=0。

不計入：程式碼區塊（含沒有閉合的）、行內程式碼、URL、email、Markdown 連結。
用程式碼區塊展示英文指令或簡體原文是正當的。
最終回答只是 JSON 值（前後可有 code fence）時不檢查。那是給程式讀的輸出，
退回會混入說明文字而無法解析。
環境變數 CHINESE_GUARD_DISABLE=1 時什麼都不做，給 claude -p 這類不給人看的輸出用。

每回合只退回一次（stop_hook_active 為真就放行），改寫後又不合格也就此打住，避免無限迴圈。

Windows 相容：stdin 一律以 UTF-8 解碼，輸出的 JSON 一律跳脫成 ASCII，
不受系統編碼（例如繁中 Windows 的 cp950）影響。

單獨測試：python chinese-guard.py --check <transcript.jsonl>
"""

import json
import os
import re
import sys
import time


def env_number(name, default, cast=int):
  """讀數字型環境變數，格式錯誤時退回預設值，不讓 hook 因設定打錯而掛掉"""
  try:
    return cast(os.environ.get(name, default))
  except ValueError:
    return cast(default)


def env_flag(name, default):
  return os.environ.get(name, default) not in ("0", "false", "no", "")


# 英文字母少於此數的本文不判定（「OK」「Done」這類短回覆放過）
MIN_LATIN = env_number("CHINESE_GUARD_MIN_LATIN", "25")
# 工具呼叫之間的多行進度報告，英文字母達此數才判定
MIN_LATIN_MID = env_number("CHINESE_GUARD_MIN_LATIN_MID", "60")
# 單行進度報告，英文字母達此數（長段落）才判定
MIN_LATIN_MID_LONG = env_number("CHINESE_GUARD_MIN_LATIN_MID_LONG", "250")
# 單行進度報告，中日文字元達此數才判定日文與簡體字
MIN_CJK_MID_LONG = env_number("CHINESE_GUARD_MIN_CJK_MID_LONG", "100")
# 英文字母數超過中日文字元數的這個倍數，視為英文為主
RATIO = env_number("CHINESE_GUARD_RATIO", "3", float)
# 假名至少要有這麼多個，且至少是漢字數的 KANA_RATIO 倍，才視為日文
MIN_KANA = env_number("CHINESE_GUARD_MIN_KANA", "10")
KANA_RATIO = env_number("CHINESE_GUARD_KANA_RATIO", "0.2", float)
# 簡體字至少要有這麼多個，且佔漢字的比例達 SIMPLIFIED_RATIO，才視為簡體中文
MIN_SIMPLIFIED = env_number("CHINESE_GUARD_MIN_SIMPLIFIED", "3")
SIMPLIFIED_RATIO = env_number("CHINESE_GUARD_SIMPLIFIED_RATIO", "0.05", float)
# 是否檢查日文、簡體字（設 0 關閉）
CHECK_JAPANESE = env_flag("CHINESE_GUARD_CHECK_JAPANESE", "1")
CHECK_SIMPLIFIED = env_flag("CHINESE_GUARD_CHECK_SIMPLIFIED", "1")
# 是否檢查「下一步」框（wrap_up）；其他工具的參數一律不檢查
CHECK_WRAP_UP = env_flag("CHINESE_GUARD_CHECK_WRAP_UP", "1")
# 等待最終回答寫入 transcript 的上限（秒）
WAIT_SECONDS = env_number("CHINESE_GUARD_WAIT", "3", float)
# 設為 1 就什麼都不做
DISABLED = os.environ.get("CHINESE_GUARD_DISABLE") == "1"

LATIN = re.compile(r"[A-Za-z]")
# 漢字：擴充 A、基本區、相容區
HAN = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
# 平假名、片假名（含長音符「ー」）
KANA = re.compile(r"[ぁ-ゖァ-ヺー]")
IGNORE = [
  # 程式碼區塊；沒有閉合的（回應中斷）也一路忽略到結尾
  re.compile(r"```.*?(?:```|\Z)", re.S),
  re.compile(r"`[^`\n]*`"),
  # Markdown 連結要比 URL 先拿掉，否則只剩下英文標題
  re.compile(r"\[[^\]]*\]\([^)]*\)"),
  re.compile(r"https?://\S+"),
  re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
]

# 只在簡體中文使用、台灣正體中文不會用的常見字。
# 刻意排除繁簡同形或在繁體中另有用法的字（例如：后、里、面、干、台、几、于、划、伙、丰、云、范）。
SIMPLIFIED = set(
  "这们说个为么来时对发会过还进开关问题实现应该设计数据库软网络间际显单击视图导错误调试项编译环条档类输页链请务优选择"
  "从东书买卖读写门马鸟鱼龙车长见贝风飞气电话语认识让记证讯论许词课谁谈议详资质赛赶迟远运边达递适连违陆阶队阳阴难颜额"
  "顺须预领频级纪约红纲纳线组细终经绍结给统继续维绩缓缩罗义习乐亏产亲亿仅仓价众传伤侠侧储兴养兽决况净凤则刚创删别刘剧"
  "劝办动励劳势区医华协卫历压厅县参双变号吗听启员呜响圆团园围场块坏坚处备头夹夺奋妇学宁宝宠审宪宽寻将层屿岁岛币帅师带"
  "帮庆废异弃张弹归录彻忆怀态总恋恶惊惯愿戏战执扩扫扬扰抚抢护报担拟拥拦拨挡挥损换据掷摄摆摇敌断无旧晋晓暂术机杀杂权杨"
  "极构枪标树样桥梦检楼横欢欧毕汇汉汤沟没泪泽洁浅测济浏浓涨润渐温湾满滚灭灯灵灾炉点热焕爱爷牵犹独狮献画畅疗盘确码础离"
  "种积称稳穷竞笔签简筑篮粮紧纠纯纸练织绘绝综缘职联聪肃胜脑脸舰艺节苏荐获营药蓝虑补装观规览觉订讨训讲访评诉译诗诚询诸"
  "谢负贡财责败货购贯费赏赞赢赵轨转轮轻载较辅辆辑逻遗郑释钟钢钱铁银销锁锅键镜闪闭闲阅陈随隐雾韩顶项顾饭饮饰馆驱验骑鲜"
  "鸡鸭黄齐龟与专业丝两严丧临丽举乌乔乡乱争亚亩仪伞伟伦伪侣侦侨俭债倾偿兰写军农冯冻减凑凯剂剑剥勋卢厌厕叠叹吓国尽声"
)

RULE = "使用者要求以繁體中文（台灣用詞）回應。"

# 工具呼叫的參數不受這個規則管：指令、程式碼、檔案內容、交給其他工具的文字
# （例如指定用其他語言說話的語音或角色台詞）都維持原本的語言
TOOL_RULE = (
  "這個規則只適用於寫給使用者看的本文；"
  "呼叫工具時的參數（指令、程式碼、檔案內容、交給工具的文字或台詞）維持原本的語言，不要因此改寫。"
)

REASON = (
  "這一回合的回應中，有不是繁體中文的段落：\n"
  "{quoted}\n"
  + RULE +
  # 每一項只列出開頭一行作為標記；曾發生 Claude 只翻譯那一行（「Status so far:」→「目前進度：」）
  # 就結束，所以明確要求整則改寫
  "上面每一項只列出不合格訊息的開頭一行，用來指出是哪一則訊息。"
  "請把每一則列出的訊息從頭到尾完整改寫成繁體中文（台灣用詞）後送出，不能只改寫列出的那一行。"
  "同一回合中其他已經用繁體中文寫好的訊息已送達使用者，不要重貼。"
  "不要寫藉口或解釋原因，之後寫給使用者的本文也全部使用繁體中文。"
  + TOOL_RULE
)

WRAP_UP_REASON = (
  "「下一步」框（recap、ask、按鈕文字）會直接顯示給使用者，但不是繁體中文：{label}。"
  + RULE +
  "請把框裡的文字改寫成繁體中文（台灣用詞）後再呼叫一次。"
  + TOOL_RULE
)


def strip_ignored(text):
  for pattern in IGNORE:
    text = pattern.sub("", text)
  return text


def classify(text, min_latin=MIN_LATIN, min_cjk=0):
  """判定本文是否不合格。不合格時回傳 (類別, 補充說明)，合格回傳 None。
  min_cjk：中日文字元少於此數時，不判定日文與簡體字。"""
  t = strip_ignored(text or "")
  latin = len(LATIN.findall(t))
  hans = HAN.findall(t)
  kana = len(KANA.findall(t))
  cjk = len(hans) + kana
  if latin >= min_latin and latin > cjk * RATIO:
    return "英文", ""
  if cjk < min_cjk:
    return None
  if CHECK_JAPANESE and kana >= MIN_KANA and kana >= len(hans) * KANA_RATIO:
    return "日文", ""
  if CHECK_SIMPLIFIED:
    simp = [c for c in hans if c in SIMPLIFIED]
    if len(simp) >= MIN_SIMPLIFIED and len(simp) >= len(hans) * SIMPLIFIED_RATIO:
      # 列出前幾個出現的簡體字，讓 Claude 知道要改哪些
      return "簡體字", "（" + "、".join(list(dict.fromkeys(simp))[:8]) + "）"
  return None


def classify_mid(text):
  """進度報告的判定：一行的前言放過，只看多行或長段落"""
  lines = [line for line in (text or "").strip().splitlines() if line.strip()]
  if len(lines) >= 2:
    return classify(text, MIN_LATIN_MID)
  return classify(text, MIN_LATIN_MID_LONG, MIN_CJK_MID_LONG)


def looks_like_json(text):
  """最終回答是 JSON 值本身（前後可有 code fence）時回傳 True"""
  t = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip())
  if not t or t[0] not in "[{":
    return False
  try:
    json.loads(t)
    return True
  except ValueError:
    return False


def is_wrap_up(name):
  """是否為桌面版「下一步」框的工具（MCP 工具，名稱帶前綴）。關閉檢查時一律回傳 False"""
  return CHECK_WRAP_UP and (name or "").endswith("wrap_up")


def wrap_up_text(tool_input):
  """把「下一步」框顯示的文字（recap、ask、按鈕）合成一段"""
  tool_input = tool_input if isinstance(tool_input, dict) else {}
  parts = [tool_input.get("recap") or "", tool_input.get("ask") or ""]
  actions = tool_input.get("actions")
  if isinstance(actions, str):
    try:
      actions = json.loads(actions)
    except ValueError:
      actions = []
  if not isinstance(actions, list):
    actions = []
  for action in actions:
    if isinstance(action, dict):
      parts += [action.get("label") or "", action.get("message") or ""]
  return "\n".join(p for p in parts if isinstance(p, str) and p)


def describe(text, result, where=""):
  """組出退回訊息中的一行：［類別］（位置）開頭一行（補充）"""
  label, detail = result
  lines = [line for line in text.strip().splitlines() if line.strip()]
  first = lines[0].strip()[:80] if lines else ""
  # 多行時標出整則的行數，讓 Claude 知道要改寫的不只是這一行
  size = "……（整則共 %d 行）" % len(lines) if len(lines) > 1 else ""
  return "［" + label + "］" + where + first + size + detail


def is_user_turn(entry):
  """是否為使用者輸入的發言（排除工具結果與 meta 的 user 項目）"""
  if entry.get("type") != "user" or entry.get("isMeta"):
    return False
  content = (entry.get("message") or {}).get("content")
  if isinstance(content, str):
    return True
  if isinstance(content, list):
    return not any(isinstance(c, dict) and c.get("type") == "tool_result" for c in content)
  return False


class Transcript:
  """增量讀取 transcript。等待最終回答時只解析新寫入的部分，長 session 也不會每次重讀整個檔案。"""

  def __init__(self, path):
    self.path = path
    self.offset = 0
    self.rest = b""
    self.entries = []

  def read(self):
    with open(self.path, "rb") as f:
      f.seek(self.offset)
      chunk = f.read()
    self.offset += len(chunk)
    lines = (self.rest + chunk).split(b"\n")
    # 最後一段可能還沒寫完，留到下次
    self.rest = lines.pop()
    for line in lines:
      entry = parse_line(line)
      if entry is not None:
        self.entries.append(entry)
    # 檔案結尾沒有換行但內容已完整時，也納入（不消耗，下次仍會重試）
    tail = parse_line(self.rest)
    return self.entries + ([tail] if tail is not None else [])


def parse_line(line):
  line = line.strip()
  if not line:
    return None
  try:
    entry = json.loads(line.decode("utf-8", errors="replace"))
  except ValueError:
    return None
  return entry if isinstance(entry, dict) else None


def turn_ended_in_text(entries):
  """本回合最新的 assistant 項目是否以本文結尾"""
  for entry in reversed(entries):
    if is_user_turn(entry):
      return False
    if entry.get("type") == "assistant":
      content = (entry.get("message") or {}).get("content") or []
      last = next((c for c in reversed(content) if isinstance(c, dict)), None)
      return bool(last) and last.get("type") == "text"
    if entry.get("type") == "user":  # 最後是工具結果＝最終回答還沒寫入
      return False
  return False


def wait_for_final(transcript, timeout=WAIT_SECONDS):
  """Stop hook 有時會在最終回答寫入 transcript 之前被呼叫，直接讀會看不到最終回答而放行。
  等到最終回答出現為止。沒呼叫工具就結束的回合一開始就以本文結尾，不必等。"""
  deadline = time.time() + timeout
  entries = transcript.read()
  while not turn_ended_in_text(entries) and time.time() < deadline:
    time.sleep(0.1)
    entries = transcript.read()
  return entries


def find_passages(entries):
  """回傳本回合不合格段落的描述清單"""
  start = 0
  for i, entry in enumerate(entries):
    if is_user_turn(entry):
      start = i + 1
  # 最終回答＝最後一次工具呼叫之後的本文；每遇到工具呼叫就重新收集
  final = []
  hits = []
  for entry in entries[start:]:
    if entry.get("type") != "assistant":
      continue
    for block in (entry.get("message") or {}).get("content") or []:
      if not isinstance(block, dict):
        continue
      kind = block.get("type")
      if kind == "tool_use":
        # 沒成為最終回答的本文是進度報告，只看有份量的
        for t in final:
          result = classify_mid(t)
          if result:
            hits.append(describe(t, result, "（進度報告）"))
        final = []
        if is_wrap_up(block.get("name")):
          text = wrap_up_text(block.get("input"))
          result = classify(text)
          if result:
            hits.append(describe(text, result, "（下一步框）"))
      elif kind == "text":
        final.append(block.get("text") or "")
      elif kind == "thinking" and entry.get("thinkingDisplay") == "updates":
        # thinkingDisplay 為 updates 時，有內容的 thinking 會以進度報告顯示在畫面上
        text = block.get("thinking") or ""
        result = classify_mid(text) if text.strip() else None
        if result:
          hits.append(describe(text, result, "（進度報告）"))
  for t in final:
    result = classify(t)
    if result:
      hits.append(describe(t, result))
  return hits


def read_stdin():
  """以 UTF-8 讀 hook 輸入，不依賴系統編碼"""
  raw = sys.stdin.buffer.read()
  if not raw.strip():
    return {}
  data = json.loads(raw.decode("utf-8", errors="replace"))
  return data if isinstance(data, dict) else {}


def emit(obj):
  # ensure_ascii=True：非 ASCII 字元一律跳脫成 \uXXXX，在 cp950 等任何編碼下都不會出錯
  sys.stdout.write(json.dumps(obj, ensure_ascii=True))
  sys.stdout.flush()


def run_check(path):
  # 一律輸出 UTF-8（Windows 主控台走 Unicode API 不受影響；導向檔案或管線時也不會因 cp950 缺字而出錯）
  try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
  except AttributeError:
    pass
  hits = find_passages(Transcript(path).read())
  print("\n".join(hits) if hits else "沒有不合格的段落")


def main():
  if len(sys.argv) == 3 and sys.argv[1] == "--check":
    run_check(sys.argv[2])
    return
  if DISABLED:
    return
  data = read_stdin()
  if data.get("hook_event_name") == "PreToolUse":
    if is_wrap_up(data.get("tool_name")):
      result = classify(wrap_up_text(data.get("tool_input")))
      if result:
        emit({"hookSpecificOutput": {
          "hookEventName": "PreToolUse",
          "permissionDecision": "deny",
          "permissionDecisionReason": WRAP_UP_REASON.format(label=result[0] + result[1]),
        }})
    return
  if data.get("stop_hook_active"):
    return
  message = data.get("last_assistant_message") or ""
  # 最終回答只是 JSON 時視為給程式讀的輸出；退回會加上說明文字，JSON 就無法解析
  if looks_like_json(message):
    return
  # last_assistant_message 也有最終回答，不必等 transcript 寫入就能判定。
  # 但那裡只有最後一則回應，回應被拆成多則時前半段要靠 transcript 檢查
  message_result = classify(message) if message else None
  hits = []
  path = data.get("transcript_path")
  if path:
    try:
      transcript = Transcript(path)
      entries = transcript.read() if message_result else wait_for_final(transcript)
      hits = find_passages(entries)
    except OSError:
      pass
  if message_result:
    line = describe(message, message_result)
    if line not in hits:
      hits.append(line)
  if hits:
    quoted = "\n".join("- " + h for h in hits[:5])
    emit({"decision": "block", "reason": REASON.format(quoted=quoted)})


if __name__ == "__main__":
  try:
    main()
  except Exception as e:  # hook 自身出錯時不要卡住使用者，只在 stderr 留下訊息
    sys.stderr.write("chinese-guard: " + repr(e) + "\n")
    sys.exit(1)
