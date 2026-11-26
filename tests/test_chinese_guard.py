"""執行：python tests/test_chinese_guard.py [--jobs N] [--shard i/n]

函式層級的案例在本行程內直接跑；hook 層級的案例每個開一個子行程，
預設以「核心數減 1」個執行緒同時跑。--shard i/n 只跑第 i 份（從 0 起算）。

子行程刻意以 PYTHONUTF8=0、UTF-8 位元組輸入執行，模擬 Claude Code 在繁中 Windows（cp950）上呼叫 hook。
"""

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "chinese-guard.py"

# 子行程的環境：清掉會影響編碼與門檻的變數，關閉 UTF-8 模式
BASE_ENV = {k: v for k, v in os.environ.items()
            if k not in ("PYTHONUTF8", "PYTHONIOENCODING") and not k.startswith("CHINESE_GUARD_")}
BASE_ENV["PYTHONUTF8"] = "0"

TMP = tempfile.mkdtemp(prefix="chinese-guard-test-")


def load_guard():
  spec = importlib.util.spec_from_file_location("chinese_guard", HOOK)
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


guard = load_guard()


# ---- transcript 項目產生器 ----

def user(text):
  return {"type": "user", "message": {"role": "user", "content": text}}


def tool_result():
  return {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]}}


def assistant(text):
  return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def tool_use():
  return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "name": "Bash", "input": {}}]}}


def tool_call(name, tool_input):
  return {"type": "assistant", "message": {"role": "assistant", "content": [
    {"type": "tool_use", "name": name, "input": tool_input}]}}


def thinking(text, display="updates"):
  return {"type": "assistant", "thinkingDisplay": display,
          "message": {"role": "assistant", "content": [{"type": "thinking", "thinking": text}]}}


def wrap_up(recap, ask=None, actions=None):
  inp = {"recap": recap}
  if ask:
    inp["ask"] = ask
  if actions is not None:
    inp["actions"] = actions
  return {"type": "assistant", "message": {"role": "assistant", "content": [
    {"type": "tool_use", "name": "mcp__ccd_turn__wrap_up", "input": inp}]}}


# ---- 執行 hook ----

def write_transcript(entries):
  fd, path = tempfile.mkstemp(suffix=".jsonl", dir=TMP)
  with os.fdopen(fd, "w", encoding="utf-8") as f:
    for e in entries:
      f.write(json.dumps(e, ensure_ascii=False) + "\n")
  return path


def call_hook(data, env=None, args=()):
  """以 UTF-8 位元組送入 hook，回傳 (解析後的輸出或 None, 原始 stdout 位元組)"""
  payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
  out = subprocess.run([sys.executable, str(HOOK), *args], input=payload, capture_output=True,
                       env={**BASE_ENV, **(env or {})}, timeout=30)
  if out.returncode != 0:
    raise AssertionError("hook 結束碼 %d：%s" % (out.returncode, out.stderr.decode("utf-8", "replace")))
  return (json.loads(out.stdout) if out.stdout.strip() else None), out.stdout


def run_stop(entries, stop_hook_active=False, last_message=None, env=None):
  data = {"hook_event_name": "Stop", "transcript_path": write_transcript(entries),
          "stop_hook_active": stop_hook_active}
  if last_message is not None:
    data["last_assistant_message"] = last_message
  return call_hook(data, env)[0]


def blocked(result):
  return bool(result and result.get("decision") == "block")


def run_pre(tool_name, tool_input, env=None):
  result, _ = call_hook({"hook_event_name": "PreToolUse", "tool_name": tool_name, "tool_input": tool_input}, env)
  return bool(result and result.get("hookSpecificOutput", {}).get("permissionDecision") == "deny")


# ---- 測試文字 ----

EN = "The build finished successfully and all tests passed, so I will publish the page now."
ZH = "建置已通過，測試也全部成功，現在就發布頁面。"
SC = "构建已经通过，测试也全部成功，现在就发布这个页面。"
JP = "ビルドが通り、テストもすべて成功したので、ページを公開します。"
EN_MID = ("I have finished reading the configuration files.\n"
          "Next I will update the build script, run the tests again, and report the results once everything passes.")
EN_ONE_LINE = "Register the page in the index after the existing entry, then publish it and check the build log."
ZH_MID = "設定檔讀完了。\n接下來修正建置腳本，再重跑一次測試。"
SC_MID = "配置文件读完了。\n接下来修改构建脚本，再重新运行一次测试。"
JP_MID = "設定ファイルを読み終えました。\n次はビルドスクリプトを直して、テストを流し直します。"


# ---- 函式層級（本行程內） ----

def unit_cases():
  c = guard.classify
  return [
    ("classify：繁中合格", c(ZH) is None),
    ("classify：英文判為英文", (c(EN) or ("",))[0] == "英文"),
    ("classify：簡體判為簡體字", (c(SC) or ("",))[0] == "簡體字"),
    ("classify：簡體字的補充列出用到的字", "这" in (c(SC) or ("", ""))[1]),
    ("classify：日文判為日文", (c(JP) or ("",))[0] == "日文"),
    ("classify：繁中裡引用短短的日文標題仍合格", c("這個專案的標題是「日本語でおｋ」，意思是請用日文回答。") is None),
    ("classify：繁中裡提到一兩個簡體字仍合格", c("簡體的「这」對應繁體的「這」，「们」對應「們」。") is None),
    ("classify：程式碼區塊裡的簡體不算", c("執行下列程式：\n```python\nprint('这是测试，现在发布这个页面')\n```") is None),
    ("classify：沒閉合的程式碼區塊也不算", c("以下是輸出：\n```\n" + EN + " " + EN) is None),
    ("classify：Markdown 連結的標題不算", c("參考：[Apple Developer Documentation](https://developer.apple.com/documentation/)") is None),
    ("classify：關閉簡體檢查的旗標有效", run_flag_off("CHINESE_GUARD_CHECK_SIMPLIFIED")),
    ("classify：關閉日文檢查的旗標有效", run_flag_off("CHINESE_GUARD_CHECK_JAPANESE")),
    ("SIMPLIFIED 不含常見的繁簡同形字", not (set("后里面干台几于划伙丰云范只才系松谷") & guard.SIMPLIFIED)),
    ("looks_like_json：JSON 與 code fence", guard.looks_like_json('{"a": 1}')
      and guard.looks_like_json('```json\n[1, 2]\n```') and not guard.looks_like_json("{" + EN)),
    ("wrap_up_text：按鈕以 JSON 字串傳入也讀得到", EN in guard.wrap_up_text(
      {"recap": ZH, "actions": json.dumps([{"label": "Go", "message": EN}])})),
    ("Transcript：結尾沒有換行也讀得到最後一筆", transcript_without_newline()),
  ]


def run_flag_off(name):
  """在子行程中關閉旗標再匯入模組，確認對應判定不再觸發"""
  text = SC if "SIMPLIFIED" in name else JP
  code = ("import importlib.util,sys;s=importlib.util.spec_from_file_location('g',sys.argv[1]);"
          "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
          "sys.exit(0 if m.classify(sys.argv[2]) is None else 1)")
  env = {**BASE_ENV, name: "0", "PYTHONUTF8": "1"}
  return subprocess.run([sys.executable, "-c", code, str(HOOK), text], env=env, timeout=30).returncode == 0


def transcript_without_newline():
  fd, path = tempfile.mkstemp(suffix=".jsonl", dir=TMP)
  with os.fdopen(fd, "w", encoding="utf-8") as f:
    f.write(json.dumps(user("a"), ensure_ascii=False) + "\n" + json.dumps(assistant(EN), ensure_ascii=False))
  return len(guard.Transcript(path).read()) == 2


# ---- hook 層級（子行程） ----

STOP_CASES = [
  ("英文本文退回", [user("發布"), assistant(EN)], True),
  ("繁中本文放行", [user("發布"), assistant(ZH)], False),
  ("簡體本文退回", [user("發布"), assistant(SC)], True),
  ("日文本文退回", [user("發布"), assistant(JP)], True),
  ("工具前的英文短句，最終回答是繁中就放行", [user("發布"), assistant("Checking the build first."
    " Then I will run the whole test suite."), tool_use(), tool_result(), assistant(ZH)], False),
  ("工具前的單行英文前言超過 60 字，最終回答是繁中仍放行", [user("發布"), assistant(EN_ONE_LINE),
    tool_use(), tool_result(), assistant(ZH)], False),
  ("工具前的單行簡體短前言放行", [user("發布"), assistant("先看一下这个文件。"), tool_use(), tool_result(),
    assistant(ZH)], False),
  ("最後一次工具之後的最終回答是英文就退回", [user("發布"), assistant(ZH), tool_use(), tool_result(),
    assistant(EN)], True),
  ("沒呼叫工具的回合混入英文段落就退回", [user("發布"), assistant(ZH), assistant(EN)], True),
  ("程式碼區塊的英文不算", [user("步驟？"), assistant("執行下列指令。\n```bash\n"
    "npm install && npm run build && npm test -- --coverage --watchAll=false\n```")], False),
  ("上一回合的英文不算", [user("a"), assistant(EN), user("b"), assistant(ZH)], False),
  ("第二次放行（避免無限迴圈）", [user("發布"), assistant(EN)], True, {"active": True}),
  ("短英文放過", [user("可以嗎？"), assistant("OK, done.")], False),
  ("工具之間的多行英文進度報告退回", [user("發布"), assistant(EN_MID), tool_use(), tool_result(),
    assistant(ZH)], True),
  ("工具之間的多行簡體進度報告退回", [user("發布"), assistant(SC_MID), tool_use(), tool_result(),
    assistant(ZH)], True),
  ("工具之間的多行日文進度報告退回", [user("發布"), assistant(JP_MID), tool_use(), tool_result(),
    assistant(ZH)], True),
  ("updates 顯示的 thinking 是英文就退回", [user("發布"), thinking(EN_MID), tool_use(), tool_result(),
    assistant(ZH)], True),
  ("updates 顯示的 thinking 是繁中就放行", [user("發布"), thinking(ZH_MID), tool_use(), tool_result(),
    assistant(ZH)], False),
  ("非 updates 的 thinking 不算", [user("發布"), thinking(EN_MID, display="summarized"), tool_use(),
    tool_result(), assistant(ZH)], False),
  ("下一步框是英文就退回", [user("發布"), wrap_up(EN, "Should I publish it now?"), tool_result(),
    assistant("No response requested.")], True),
  ("下一步框是簡體就退回", [user("發布"), wrap_up(SC, "现在发布吗？"), tool_result(),
    assistant("（無回應）")], True),
  ("下一步框是繁中就放行", [user("發布"), wrap_up(ZH, "要發布嗎？", [{"label": "發布"}]), tool_result(),
    assistant("（無回應）")], False),
  # 工具呼叫的參數不檢查，語言維持原樣
  ("語音工具的日文台詞不檢查", [user("發布"), tool_call("mcp__mascot__speak", {"text": JP}),
    tool_result(), assistant(ZH)], False),
  ("Bash 的英文指令不檢查", [user("發布"), tool_call("Bash", {"command": EN, "description": EN}),
    tool_result(), assistant(ZH)], False),
  ("寫入檔案的簡體內容不檢查", [user("發布"), tool_call("Write", {"file_path": "a.txt", "content": SC * 3}),
    tool_result(), assistant(ZH)], False),
]


def stop_case(case):
  name, entries, expect = case[:3]
  opts = case[3] if len(case) > 3 else {}
  result = run_stop(entries, stop_hook_active=opts.get("active", False))
  # 第二次放行的案例：expect 表示「沒有 active 時會退回」，active 時應放行
  if opts.get("active"):
    return not blocked(result)
  return blocked(result) == expect


def late_write_case():
  """最終回答比 hook 啟動晚寫入，也要等到再判定"""
  path = write_transcript([user("發布"), tool_use(), tool_result()])
  late = json.dumps(assistant(SC), ensure_ascii=False) + "\n"

  def append():
    with open(path, "a", encoding="utf-8") as f:
      f.write(late)
  threading.Timer(0.5, append).start()
  result, _ = call_hook({"hook_event_name": "Stop", "transcript_path": path})
  return blocked(result)


def last_message_fast_case():
  t0 = time.time()
  result = run_stop([user("發布"), tool_use(), tool_result()], last_message=EN)
  return blocked(result) and time.time() - t0 < 2.5


def reason_case():
  """退回訊息要求繁中，並列出類別與簡體字"""
  result = run_stop([user("發布"), assistant(SC)])
  reason = (result or {}).get("reason", "")
  return ("繁體中文" in reason and "［簡體字］" in reason and "这" in reason
          and "工具時的參數" in reason and "維持原本的語言" in reason)


def ascii_output_case():
  """輸出必須是純 ASCII，cp950 環境下也不會編碼錯誤"""
  _, raw = call_hook({"hook_event_name": "Stop", "transcript_path": write_transcript([user("發布"), assistant(JP)])})
  try:
    raw.decode("ascii")
  except UnicodeDecodeError:
    return False
  return b"block" in raw


def multiline_reason_case():
  """多行的英文回答只列出開頭一行時，退回訊息必須要求整則改寫，並標出整則有幾行。
  曾發生：只列出「Status so far:」，Claude 就只把那一行翻成「目前進度：」。"""
  text = "Status so far:\n\n- " + EN + "\n- " + EN + "\n- " + EN
  result = run_stop([user("進度？"), tool_use(), tool_result(), assistant(text)], last_message=text)
  reason = (result or {}).get("reason", "")
  return ("［英文］Status so far:" in reason and "整則共 4 行" in reason
          and "從頭到尾" in reason and "不能只改寫列出的那一行" in reason)


def wrap_up_reason_case():
  result, _ = call_hook({"hook_event_name": "PreToolUse", "tool_name": "mcp__ccd_turn__wrap_up",
                         "tool_input": {"recap": EN}})
  reason = (result or {}).get("hookSpecificOutput", {}).get("permissionDecisionReason", "")
  return "維持原本的語言" in reason and "［" not in reason and "英文" in reason


def disable_case():
  result = run_stop([user("發布"), assistant(EN)], last_message=EN, env={"CHINESE_GUARD_DISABLE": "1"})
  return result is None


def empty_stdin_case():
  out = subprocess.run([sys.executable, str(HOOK)], input=b"", capture_output=True, env=BASE_ENV, timeout=30)
  return out.returncode == 0 and not out.stdout.strip()


def check_cli_case():
  path = write_transcript([user("發布"), assistant(SC)])
  out = subprocess.run([sys.executable, str(HOOK), "--check", path], capture_output=True, env=BASE_ENV, timeout=30)
  text = out.stdout.decode("utf-8", errors="replace")
  return out.returncode == 0 and "簡體字" in text


HOOK_CASES = (
  [("Stop：" + c[0], (lambda c=c: stop_case(c))) for c in STOP_CASES]
  + [
    ("Stop：最終回答晚寫入也會等到並退回", late_write_case),
    ("Stop：last_assistant_message 是英文時不等 transcript 直接退回", last_message_fast_case),
    ("Stop：last_assistant_message 是繁中就放行",
     lambda: run_stop([user("發布"), assistant(ZH)], last_message=ZH) is None),
    ("Stop：last_assistant_message 是繁中，但被拆開的前半是英文，靠 transcript 退回",
     lambda: blocked(run_stop([user("發布"), assistant(EN), assistant(ZH)], last_message=ZH))),
    ("Stop：最終回答只有 JSON 就放行", lambda: not blocked(run_stop(
      [user("JSON"), tool_use(), tool_result()],
      last_message=json.dumps({"status": "ok", "items": [{"title": "週報", "summary": EN}]})))),
    ("Stop：JSON 前面有英文說明就退回", lambda: blocked(run_stop(
      [user("JSON"), tool_use(), tool_result()], last_message=EN + "\n" + json.dumps({"a": EN})))),
    ("Stop：退回訊息要求繁中並列出簡體字", reason_case),
    ("Stop：多行回答只列開頭時，要求整則改寫", multiline_reason_case),
    ("Stop：輸出為純 ASCII（cp950 安全）", ascii_output_case),
    ("Stop：CHINESE_GUARD_DISABLE=1 時放行", disable_case),
    ("Stop：stdin 為空也不出錯", empty_stdin_case),
    ("CLI：--check 列出不合格段落", check_cli_case),
    ("PreToolUse：英文框 deny", lambda: run_pre("mcp__ccd_turn__wrap_up",
      {"recap": EN, "ask": "Publish now?", "actions": [{"label": "Publish", "message": "Yes, go ahead."}]})),
    ("PreToolUse：簡體框 deny", lambda: run_pre("mcp__ccd_turn__wrap_up", {"recap": SC, "ask": "现在发布吗？"})),
    ("PreToolUse：按鈕以 JSON 字串傳入也檢查", lambda: run_pre("mcp__ccd_turn__wrap_up",
      {"recap": "準備好發布了。", "actions": json.dumps([{"label": "Go", "message": EN}])})),
    ("PreToolUse：繁中框放行", lambda: not run_pre("mcp__ccd_turn__wrap_up",
      {"recap": ZH, "ask": "要發布嗎？", "actions": [{"label": "發布"}]})),
    ("PreToolUse：其他工具不檢查", lambda: not run_pre("Bash", {"command": EN})),
    ("PreToolUse：語音工具的日文台詞不檢查", lambda: not run_pre("mcp__mascot__speak", {"text": JP})),
    ("PreToolUse：CHINESE_GUARD_CHECK_WRAP_UP=0 時下一步框也不檢查", lambda: not run_pre(
      "mcp__ccd_turn__wrap_up", {"recap": EN}, env={"CHINESE_GUARD_CHECK_WRAP_UP": "0"})),
    ("PreToolUse：退回訊息要求工具參數維持原語言", wrap_up_reason_case),
  ]
)


def safe(fn):
  try:
    return bool(fn()), ""
  except Exception as e:  # 單一案例出錯不影響其他案例
    return False, repr(e)


def main():
  parser = argparse.ArgumentParser()
  parser.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 1))
  parser.add_argument("--shard", default="0/1", help="只跑第 i/n 份，例如 0/4")
  args = parser.parse_args()
  index, total = (int(x) for x in args.shard.split("/"))

  cases = [(n, (lambda ok=ok: ok)) for n, ok in unit_cases()] + HOOK_CASES
  cases = [c for i, c in enumerate(cases) if i % total == index]

  with ThreadPoolExecutor(max_workers=args.jobs) as pool:
    results = list(pool.map(lambda c: (c[0],) + safe(c[1]), cases))

  failed = 0
  for name, ok, err in results:
    failed += not ok
    print(("✓ " if ok else "✗ ") + name + (("  " + err) if err else ""))
  print("\n%d 個案例，失敗 %d 個（%d 個執行緒）" % (len(results), failed, args.jobs))
  sys.exit(1 if failed else 0)


if __name__ == "__main__":
  try:
    sys.stdout.reconfigure(errors="replace")
  except AttributeError:
    pass
  main()
