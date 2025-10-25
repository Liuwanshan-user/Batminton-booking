#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
SJTU Sports Auto-Booker — Manual Login + No-Label Grid (Strict Success Check)
---------------------------------------------------------------------------
使用方式：
1) 先手动登录 https://sports.sjtu.edu.cn/ 并进入羽毛球预约网格页面（能看到日期页签+时间行+格子）。
2) 运行脚本（可用 --help 查看完整参数）：
   - 立刻测试：python this_script.py --now
   - 指定日期偏移/时间段：python this_script.py --date-offset 6 --slots "18:00,19:00" --courts "2,3"
   - 等到 12:00：python this_script.py
3) 脚本会在目标时间自动：
   - 选择“今天 + DATE_OFFSET_DAYS”的日期页签
   - 遍历 PREFERRED_SLOTS（时间行）与 PREFERRED_COURTS（该行第 N 块）尝试点击
   - 若出现确认弹窗会点“确认/确定”
   - 只有当出现明确成功提示，或右侧“选中的/金额/下单按钮”状态真实变化，才判定成功；否则继续尝试

注意：
- 你可以在脚本顶部配置日期偏移、时间行优先级以及场地优先级。
- 仍然不处理 jAccount 验证码；通过你手动登录规避。
"""

import argparse
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Iterable, List

import pytz
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

# ====================== 配置 ======================
@dataclass(frozen=True)
class BookingConfig:
    start_url: str = "https://sports.sjtu.edu.cn/pc/?locale=zh#/"
    date_offset_days: int = 7
    open_time_str: str = "20:54:40"
    preferred_slots: List[str] = None
    preferred_courts: List[int] = None
    click_retries: int = 3
    headless: bool = False

    def with_defaults(self) -> "BookingConfig":
        """填充可变默认值，避免在 dataclass 定义时共享列表。"""
        return replace(
            self,
            preferred_slots=list(self.preferred_slots or [
                "18:00",
                "19:00",
                "20:00",
                "14:00",
                "15:00",
                "16:00",
                "17:00",
            ]),
            preferred_courts=list(self.preferred_courts or [1, 2, 3, 4, 5, 6, 7, 8, 9]),
        )
# ==================================================

TZ = pytz.timezone("Asia/Shanghai")


def _split_list(raw: str, *, type_=str) -> List:
    """将逗号/空格分隔的字符串转换成列表，过滤空项并转成目标类型。"""
    if not raw:
        return []
    if isinstance(raw, Iterable) and not isinstance(raw, str):
        return [type_(item) for item in raw]
    parts = [seg.strip() for seg in str(raw).replace("/", ",").split(",")]
    return [type_(p) for p in parts if p]


def _positive_int(value: str) -> int:
    ivalue = int(value)
    if ivalue <= 0:
        raise argparse.ArgumentTypeError("必须为正整数")
    return ivalue

def log(m):
    print(f"[{datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')}] {m}", flush=True)

def build_driver(config: BookingConfig):
    opts = Options()
    if config.headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1400,1000")
    opts.add_argument("--lang=zh-CN")
    svc = ChromeService(ChromeDriverManager().install())
    d = webdriver.Chrome(service=svc, options=opts)
    d.implicitly_wait(2)
    return d

def wait_until(dt):
    log(f"等待开放时间：{dt.strftime('%Y-%m-%d %H:%M:%S')}")
    while True:
        now = datetime.now(TZ)
        if now >= dt:
            break
        remain = (dt - now).total_seconds()
        time.sleep(0.2 if remain < 3 else 1.0 if remain < 30 else 5.0)

def next_open_time(s: str):
    today = datetime.now(TZ).date()
    parts = [int(p) for p in s.split(":")]
    if len(parts) == 2:
        h, m = parts
        x = 0
    elif len(parts) == 3:
        h, m, x = parts
    else:
        raise ValueError("开放时间格式应为 HH:MM 或 HH:MM:SS")
    cand = TZ.localize(datetime(today.year, today.month, today.day, h, m, x))
    return cand if cand > datetime.now(TZ) else cand + timedelta(days=1)

def chinese_weekday(dt):
    return ["周一","周二","周三","周四","周五","周六","周日"][dt.weekday()]

def click_date_tab(driver, target_date):
    """更稳的中文日期匹配：10月31日、10月31日(周五)、10-31、2025-10-31 等。"""
    mm, dd, wd = target_date.month, target_date.day, chinese_weekday(target_date)
    candidates = [
        f"{mm}月{dd}日 ({wd})", f"{mm}月{dd}日({wd})", f"{mm}月{dd}日",
        f"{mm:02d}月{dd:02d}日",
        target_date.strftime("%m-%d"),
        target_date.strftime("%Y-%m-%d"),
    ]
    # 允许日期条需要横向滚动
    for _ in range(8):
        for txt in candidates:
            try:
                el = WebDriverWait(driver, 0.9).until(
                    EC.element_to_be_clickable((By.XPATH, f"//*[contains(normalize-space(text()), '{txt}')]"))
                )
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                el.click()
                log(f"已点击日期：{txt}")
                return True
            except Exception:
                pass
        # 向右轻微滚动页面，帮助日期条露出
        try:
            driver.execute_script("document.scrollingElement.scrollLeft += 320;")
        except Exception:
            pass
    log("未找到目标日期页签。")
    return False

# ---------- 关键：严格判断是否“真的选择成功/已下单成功” ----------
STRICT_CHECK_JS = r"""
/*
 返回字典结构：
 {
   status: "OK_SELECTED" | "TIME_NOT_FOUND" | "ROW_OR_BUTTON_NOT_FOUND" | "CLICK_NO_EFFECT" | "BUTTON_DISABLED",
   before: { selectedCount, amount, submitEnabled },
   after:  { selectedCount, amount, submitEnabled },
   info: "extra text"
 }
 逻辑：
 1) 找到左侧时间标签（精确等于 HH:MM）。
 2) 向上聚合到整行，取其中第 courtIndex 个“可点击且可见”的按钮（排除 disabled/aria-disabled/不可见）。
 3) 记录点击前的“右侧选中数量/金额/下单按钮可用状态”，点击，再记录点击后的差异。
 4) 只有当 after.selectedCount > before 或 after.amount > before.amount 或 submitEnabled 由 False->True
    才视为 “OK_SELECTED”。否则 CLICK_NO_EFFECT。
*/
(function(){
  const timeText = arguments[0];
  const courtIndex = Math.max(1, parseInt(arguments[1]||1));

  function norm(s){ return String(s||'').trim().replace(/\s+/g,''); }
  function isVisible(el){ return !!(el && el.offsetParent !== null); }
  function isEnabled(btn){
    if(!btn) return false;
    if(btn.disabled) return false;
    const aria = btn.getAttribute('aria-disabled');
    if(aria && aria.toString() === 'true') return false;
    const cls = (btn.className||'').toString();
    if(/disabled|不可|满|sold|unavailable/.test(cls)) return false;
    return isVisible(btn);
  }

  function getPanelState(){
    // 尝试抓右侧信息：选中数量、金额、下单按钮状态
    let selectedCount = 0, amount = 0.0, submitEnabled = false;

    // 1) 选中数量（寻找“选中的/合计/已选”等容器的数字）
    try{
      const t = document.body.innerText || '';
      const m1 = t.match(/选中的?\D*(\d+)/);
      if(m1) selectedCount = parseInt(m1[1],10) || 0;
    }catch(e){}

    // 2) 金额（匹配形如 ¥12 或 ￥12 或 12元）
    try{
      const t = (document.querySelector('aside, .right, .detail, .order')||document.body).innerText || '';
      const m2 = t.match(/[¥￥]\s*([0-9]+(\.[0-9]+)?)/) || t.match(/([0-9]+(\.[0-9]+)?)\s*元/);
      if(m2) amount = parseFloat(m2[1]) || 0.0;
    }catch(e){}

    // 3) 立即下单按钮是否可点
    try{
      const btns = Array.from(document.querySelectorAll('button')).filter(b => /下单|预约|提交|支付/.test(b.textContent||''));
      const btn = btns[0];
      submitEnabled = !!(btn && !btn.disabled && btn.offsetParent !== null);
    }catch(e){}

    return {selectedCount, amount, submitEnabled};
  }

  function findTimeNode(){
    const nodes = Array.from(document.querySelectorAll('body *'));
    const timeNodes = nodes.filter(e => /^\d{1,2}:\d{2}$/.test((e.textContent||'').trim()));
    return timeNodes.find(n => norm(n.textContent) === norm(timeText)) || null;
  }

  const before = getPanelState();
  const tnode = findTimeNode();
  if(!tnode) return {status:'TIME_NOT_FOUND', before, after:before, info:'time label not found'};

  // 聚合到包含多个按钮的行
  let row = tnode;
  let lastBtns = [];
  for(let i=0;i<8;i++){
    if(!row) break;
    const btns = Array.from(row.querySelectorAll('button'));
    const usable = btns.filter(b => isEnabled(b));
    if(usable.length > 0){
      lastBtns = usable;
      break;
    }
    row = row.parentElement;
  }
  if(lastBtns.length === 0){
    return {status:'ROW_OR_BUTTON_NOT_FOUND', before, after:before, info:'no clickable buttons in row'};
  }
  const idx = Math.min(lastBtns.length, courtIndex) - 1;
  const btn = lastBtns[idx];
  if(!isEnabled(btn)) return {status:'BUTTON_DISABLED', before, after:before, info:'button disabled'};

  // 点击
  btn.scrollIntoView({block:'center'});
  btn.click();

  // 等待UI反应
  const t0 = performance.now();
  while(performance.now() - t0 < 1200){
    // 轻微延时
    // eslint-disable-next-line no-empty
  }
  const after = getPanelState();

  // 判断是否“真的产生了选择效果”
  const changed = (after.selectedCount > before.selectedCount) || (after.amount > before.amount) || (!before.submitEnabled && after.submitEnabled);
  return {status: changed ? 'OK_SELECTED' : 'CLICK_NO_EFFECT', before, after, info:`btnCount=${lastBtns.length}`};
})();
"""

def strict_select_slot(driver, time_text, court_index):
    """用 JS 执行严格选择；返回 True 表示“选择产生了实际效果”"""
    try:
        res = driver.execute_script(STRICT_CHECK_JS, time_text, court_index)
        status = res.get("status")
        log(f"时间{time_text} 第{court_index}块 -> {status} | before={res.get('before')} after={res.get('after')} info={res.get('info')}")
        return status == "OK_SELECTED"
    except Exception as e:
        log(f"JS 执行失败：{e}")
        return False

def confirm_if_needed(driver):
    """尝试点击确认/确定；若无弹窗则忽略"""
    try:
        confirm = WebDriverWait(driver, 1.5).until(
            EC.element_to_be_clickable((By.XPATH, "//button[contains(., '确认') or contains(., '确定')]"))
        )
        confirm.click()
        log("已点击确认/确定")
        return True
    except Exception:
        return False

def wait_success_toast(driver, timeout=4):
    """等待明确成功提示；若出现“已满/不可选/失败/库存不足”等提示，立即判定失败"""
    bad_words = ["已满", "不可选", "失败", "无可用", "库存不足", "预约次数已用尽", "超过限额"]
    good_words = ["成功", "已预约", "下单成功", "预约成功"]
    end = time.time() + timeout
    last_text = ""
    while time.time() < end:
        try:
            txt = driver.find_element(By.TAG_NAME, "body").text
            last_text = txt
            if any(w in txt for w in bad_words):
                log("检测到站点返回失败信息：{}".format([w for w in bad_words if w in txt]))
                return False
            if any(w in txt for w in good_words):
                return True
        except Exception:
            pass
        time.sleep(0.15)
    # 超时：不算成功
    log("未检测到明确成功提示。最近页面文本片段：{}".format(last_text[:60].replace("\n"," ")))
    return False

def booking_flow(driver, config: BookingConfig):
    target_date = datetime.now(TZ) + timedelta(days=config.date_offset_days)
    log(
        "准备预约日期：{} (今天+{})".format(
            target_date.strftime("%Y-%m-%d"), config.date_offset_days
        )
    )
    if not click_date_tab(driver, target_date):
        log("❌ 日期未选中，无法继续。")
        return

    # 遍历时间行与场地序号
    for t in config.preferred_slots:
        for c in config.preferred_courts:
            ok = False
            for _ in range(config.click_retries):
                if strict_select_slot(driver, t, c):
                    ok = True
                    break
                time.sleep(0.05)
            if not ok:
                continue

            # 可能弹出确认
            confirm_if_needed(driver)

            # 严格等待“明确成功提示/右侧状态变化”
            if wait_success_toast(driver, timeout=4):
                log(f"✅ 预约成功：{t}  第{c}块")
                return
            else:
                log("⚠ 该格子最终判定失败，继续尝试下一组合。")

    log("❌ 全部时间与场地组合尝试完毕，未成功。")

def parse_args(argv):
    default_config = BookingConfig().with_defaults()
    parser = argparse.ArgumentParser(
        description="SJTU 体育馆羽毛球自动预约脚本（需手动登录后使用）"
    )
    parser.add_argument("--now", action="store_true", help="立即执行，不等待开放时间")
    parser.add_argument("--start-url", default=default_config.start_url, help="预约页面地址")
    parser.add_argument(
        "--date-offset",
        type=_positive_int,
        default=default_config.date_offset_days,
        help="预约今天+N天的日期 (默认: %(default)s)",
    )
    parser.add_argument(
        "--open-time",
        default=default_config.open_time_str,
        help="每日开放时间，格式 HH:MM 或 HH:MM:SS (默认: %(default)s)",
    )
    parser.add_argument(
        "--slots",
        default=",".join(default_config.preferred_slots),
        help="优先尝试的时间行，逗号分隔 (默认: %(default)s)",
    )
    parser.add_argument(
        "--courts",
        default=",".join(str(x) for x in default_config.preferred_courts),
        help="对应时间行内优先尝试的场地序号，逗号分隔 (默认: %(default)s)",
    )
    parser.add_argument(
        "--click-retries",
        type=_positive_int,
        default=default_config.click_retries,
        help="点击失败重试次数 (默认: %(default)s)",
    )
    parser.add_argument(
        "--headless",
        dest="headless",
        action="store_true",
        default=default_config.headless,
        help="以无头模式运行浏览器",
    )
    parser.add_argument(
        "--show-browser",
        dest="headless",
        action="store_false",
        help="显示浏览器界面 (默认)",
    )

    args = parser.parse_args(argv)
    config = BookingConfig(
        start_url=args.start_url,
        date_offset_days=args.date_offset,
        open_time_str=args.open_time,
        preferred_slots=_split_list(args.slots),
        preferred_courts=_split_list(args.courts, type_=int),
        click_retries=args.click_retries,
        headless=args.headless,
    ).with_defaults()
    return args, config


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    args, config = parse_args(argv)
    driver = build_driver(config)
    try:
        driver.get(config.start_url)
        log("请手动登录并停留在羽毛球网格页面（能看到日期页签与时间行）。")
        input("已到达页面后按 Enter 继续... ")

        if args.now:
            log("立即执行 (--now)")
            booking_flow(driver, config)
        else:
            target_dt = next_open_time(config.open_time_str)
            wait_until(target_dt)
            booking_flow(driver, config)

        log("完成，8 秒后关闭浏览器。")
        time.sleep(8)
    finally:
        try:
            driver.quit()
        except Exception:
            pass

if __name__ == "__main__":
    main()
