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
   - 根据配置重新打开主站并导航到指定场馆/活动（默认：气膜体育中心 → 羽毛球）
   - 选择“今天 + DATE_OFFSET_DAYS”的日期页签
   - 遍历 PREFERRED_SLOTS（时间行）与 PREFERRED_COURTS（该行第 N 块）尝试点击
   - 若出现确认弹窗会点“确认/确定”
   - 只有当出现明确成功提示，或右侧“选中的/金额/下单按钮”状态真实变化，才判定成功；否则继续尝试

注意：
- 你可以在脚本顶部配置日期偏移、时间行优先级以及场地优先级。
- 仍然不处理 jAccount 验证码；通过你手动登录规避。
"""

import argparse
import json
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Iterable, List

import pytz
from selenium import webdriver
from selenium.common.exceptions import StaleElementReferenceException, TimeoutException
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
    venue_name: str = "南区体育馆"
    activity_name: str = "乒乓球"
    preferred_slots: List[str] = None
    preferred_courts: List[int] = None
    click_retries: int = 3
    headless: bool = False
    debug: bool = False

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


def wait_document_ready(driver, timeout=12):
    try:
        WebDriverWait(driver, timeout).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        return True
    except TimeoutException:
        log("⚠ 页面在指定时间内未完全加载。")
        return False


def is_on_grid_page(driver) -> bool:
    try:
        body_text = driver.find_element(By.TAG_NAME, "body").text
    except Exception:
        body_text = driver.page_source
    keywords = ["选中的", "立即下单", "预约", "金额"]
    return any(k in body_text for k in keywords)


def wait_for_grid_ready(driver, timeout=12):
    keywords = ["选中的", "立即下单", "预约", "金额"]
    try:
        WebDriverWait(driver, timeout).until(
            lambda d: any(k in (d.page_source or "") for k in keywords)
        )
        log("✅ 检测到预约页面关键字，认为已到达网格页面。")
        return True
    except TimeoutException:
        log("⚠ 未能在限定时间内确认预约网格加载完成。")
        return False


def _click_element_by_text(driver, target_text: str, description: str, timeout=10):
    if not target_text:
        return True
    log(f"查找{description}：{target_text}")
    xpaths = [
        f"//button[normalize-space()='{target_text}']",
        f"//a[normalize-space()='{target_text}']",
        f"//span[normalize-space()='{target_text}']",
        f"//div[normalize-space()='{target_text}']",
        f"//h3[contains(normalize-space(), '{target_text}')]",
        f"//*[@role='tab' and contains(normalize-space(), '{target_text}')]",
        f"//*[@role='button' and contains(normalize-space(), '{target_text}')]",
        f"//p[contains(normalize-space(), '{target_text}')]",
    ]
    end = time.time() + timeout
    while time.time() < end:
        for xp in xpaths:
            try:
                elements = driver.find_elements(By.XPATH, xp)
            except Exception:
                continue
            for el in elements:
                try:
                    if not el.is_displayed():
                        continue
                except StaleElementReferenceException:
                    continue
                try:
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                except Exception:
                    pass
                try:
                    el.click()
                except Exception:
                    try:
                        driver.execute_script("arguments[0].click();", el)
                    except Exception:
                        continue
                log(f"已点击{description}：{target_text}")
                time.sleep(0.4)
                return True
        time.sleep(0.3)
    log(f"❌ 未能点击{description}：{target_text}")
    return False


def _click_card_heading(driver, heading_text: str, description: str, timeout=10):
    if not heading_text:
        return True
    try:
        heading = WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located(
                (By.XPATH, f"//h3[contains(normalize-space(), '{heading_text}')]" )
            )
        )
    except TimeoutException:
        log(f"❌ 未找到{description}卡片：{heading_text}")
        return False

    try:
        container = heading.find_element(By.XPATH, "./ancestor::*[contains(@class, 'el-card')][1]")
    except Exception:
        try:
            container = heading.find_element(By.XPATH, "./ancestor::li[1]")
        except Exception:
            container = heading

    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", container)

    candidates = []
    try:
        candidates.extend(container.find_elements(By.CSS_SELECTOR, "a,button"))
    except Exception:
        pass
    candidates.append(container)
    if heading not in candidates:
        candidates.append(heading)

    for el in candidates:
        try:
            if not el.is_displayed():
                continue
        except Exception:
            pass
        for clicker in (el.click, lambda e=el: driver.execute_script("arguments[0].click();", e)):
            try:
                clicker()
                log(f"已点击{description}卡片：{heading_text}")
                return True
            except Exception:
                continue

    log(f"❌ 无法点击{description}卡片：{heading_text}")
    return False


def _search_and_click_card(driver, keyword: str) -> bool:
    if not keyword:
        return True
    log(f"使用搜索定位场馆：{keyword}")
    try:
        search_input = WebDriverWait(driver, 6).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, "input[placeholder*='场馆名称']"))
        )
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", search_input)
        try:
            search_input.clear()
        except Exception:
            driver.execute_script("arguments[0].value='';", search_input)
        search_input.send_keys(keyword)
        try:
            search_btn = search_input.find_element(By.XPATH, "../../div[@class='el-input-group__append']//button")
        except Exception:
            search_btn = driver.find_element(By.CSS_SELECTOR, ".searchInput button")
        search_btn.click()
        time.sleep(0.6)
    except Exception as exc:
        log(f"❌ 搜索框操作失败：{exc}")
        return False

    return _click_card_heading(driver, keyword, "场馆")


def _switch_to_new_window(driver, previous_handles):
    try:
        current_handles = driver.window_handles
    except Exception:
        return False
    for handle in current_handles:
        if handle not in previous_handles:
            driver.switch_to.window(handle)
            return True
    return False


def navigate_to_activity(driver, config: BookingConfig, *, force_home=False) -> bool:
    if is_on_grid_page(driver):
        log("检测到当前已在预约网格页面。")
        return True

    def _navigate_from_current_page():
        if is_on_grid_page(driver):
            return True
        handles_before = list(driver.window_handles)
        if not (
            _click_element_by_text(driver, config.venue_name, "场馆")
            or _click_card_heading(driver, config.venue_name, "场馆")
            or _search_and_click_card(driver, config.venue_name)
        ):
            return False
        time.sleep(0.8)
        if _switch_to_new_window(driver, handles_before):
            wait_document_ready(driver, timeout=12)
        if config.activity_name and not _click_element_by_text(driver, config.activity_name, "活动"):
            return False
        wait_for_grid_ready(driver, timeout=12)
        return is_on_grid_page(driver)

    if force_home:
        log(f"打开预约主页面：{config.start_url}")
        driver.get(config.start_url)
        wait_document_ready(driver, timeout=12)
        try:
            driver.switch_to.window(driver.window_handles[-1])
        except Exception:
            pass

    if _navigate_from_current_page():
        log(f"✅ 已导航到目标页面：{config.venue_name} → {config.activity_name}")
        return True

    log("当前页面未找到目标场馆/活动，尝试返回主页面重新定位。")
    driver.get(config.start_url)
    wait_document_ready(driver, timeout=12)
    try:
        driver.switch_to.window(driver.window_handles[-1])
    except Exception:
        pass
    if _navigate_from_current_page():
        log(f"✅ 已导航到目标页面：{config.venue_name} → {config.activity_name}")
        return True

    log("❌ 自动导航到目标场馆/活动失败，请手动检查页面。")
    return False


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

# ---------- 关键：严格判断是否"真的选择成功/已下单成功" ----------
STRICT_CHECK_JS = r"""
/*
  execute_async_script 回调版，返回 JSON 字符串或对象：
  {
    status: "OK_SELECTED" | "TIME_NOT_FOUND" | "ROW_OR_BUTTON_NOT_FOUND" | "CLICK_NO_EFFECT" | "BUTTON_DISABLED" | "JS_EXCEPTION",
    before: { selectedCount, amount, submitEnabled },
    after:  { selectedCount, amount, submitEnabled },
    info: "extra text"
  }
*/
var timeText = arguments[0];
var courtIndex = Math.max(1, parseInt(arguments[1] || 1, 10));
var done = arguments[arguments.length - 1];

function finish(payload) {
  try {
    done(JSON.stringify(payload));
  } catch (err) {
    try {
      done(payload);
    } catch (_) {
      done(null);
    }
  }
}

function norm(s) { return String(s || '').trim().replace(/\s+/g, ''); }
function isVisible(el) { return !!(el && el.offsetParent !== null); }

function getPanelState() {
  var selectedCount = 0, amount = 0.0, submitEnabled = false;
  try {
    var t = document.body.innerText || '';
    var m1 = t.match(/选中的?[^\d]*?(\d+)(?:\s*个)?/);
    if (m1) {
      var num = parseInt(m1[1], 10);
      if (num >= 0 && num < 100) selectedCount = num;
    }
  } catch (err) {}

  try {
    var textContainer = document.querySelector('aside, .right, .detail, .order, .drawerStyle');
    var t2 = (textContainer || document.body).innerText || '';
    var m2 = t2.match(/[¥￥]\s*([0-9]+(\.[0-9]+)?)/) || t2.match(/([0-9]+(\.[0-9]+)?)\s*元/);
    if (m2) amount = parseFloat(m2[1]) || 0.0;
  } catch (err) {}

  try {
    var btns = Array.from(document.querySelectorAll('button')).filter(function (b) {
      return /下单|预约|提交|支付/.test((b.textContent || ''));
    });
    var btn = btns[0];
    submitEnabled = !!(btn && !btn.disabled && btn.offsetParent !== null);
  } catch (err) {}

  return { selectedCount: selectedCount, amount: amount, submitEnabled: submitEnabled };
}

try {
  var before = getPanelState();
  var debugInfo = ['🔍 开始查找时间:' + timeText + ',场地:' + courtIndex];

  // 新策略：处理左右分离的布局
  // 1. 找到左侧时间列表 ul.leftUl
  var leftUl = document.querySelector('ul.leftUl, ul.leftUl.fl');
  if (!leftUl) {
    finish({ status: 'TIME_NOT_FOUND', before: before, after: before, info: 'leftUl not found' });
    return;
  }

  // 2. 在左侧列表中找到对应时间的索引
  var timeItems = Array.from(leftUl.querySelectorAll('li'));
  var timeIndex = -1;
  for (var i = 0; i < timeItems.length; i++) {
    if (norm(timeItems[i].textContent) === norm(timeText)) {
      timeIndex = i;
      break;
    }
  }

  if (timeIndex === -1) {
    debugInfo.push('❌ 在左侧时间列表中未找到时间:' + timeText);
    finish({ status: 'TIME_NOT_FOUND', before: before, after: before, info: debugInfo.join(' | ') });
    return;
  }

  debugInfo.push('✓ 找到时间索引:' + timeIndex + '/' + timeItems.length);

  // 3. 找到右侧座位容器
  var tablesDiv = document.querySelector('div.tables, div.tables.fl');
  if (!tablesDiv) {
    debugInfo.push('❌ 未找到座位容器 div.tables');
    finish({ status: 'ROW_OR_BUTTON_NOT_FOUND', before: before, after: before, info: debugInfo.join(' | ') });
    return;
  }

  // 4. 获取所有座位行（每个 div.clearfix 是一行）
  var seatRows = Array.from(tablesDiv.querySelectorAll('div.clearfix'));
  debugInfo.push('✓ 找到座位行数:' + seatRows.length);

  if (timeIndex >= seatRows.length) {
    debugInfo.push('❌ 时间索引超出座位行范围:' + timeIndex + '>=' + seatRows.length);
    finish({ status: 'ROW_OR_BUTTON_NOT_FOUND', before: before, after: before, info: debugInfo.join(' | ') });
    return;
  }

  // 5. 获取对应时间行的所有座位
  var targetRow = seatRows[timeIndex];
  var allSeats = Array.from(targetRow.querySelectorAll('div.seat'));
  debugInfo.push('✓ 该行座位总数:' + allSeats.length);

  // 6. 过滤出可用座位（排除 bought-seat）
  var availableSeats = [];
  var boughtCount = 0;
  var unselectedCount = 0;

  for (var j = 0; j < allSeats.length; j++) {
    var seat = allSeats[j];
    var innerSeat = seat.querySelector('.inner-seat');
    if (!innerSeat) continue;

    var innerClass = innerSeat.className || '';

    if (/bought-seat|bought|已购|已订/.test(innerClass)) {
      boughtCount++;
      continue;
    }

    if (/unselected-seat|available|可选|空闲/.test(innerClass) && isVisible(seat)) {
      availableSeats.push(seat);
      unselectedCount++;
    }
  }

  debugInfo.push('📊 座位统计: 总数=' + allSeats.length + ',已订=' + boughtCount + ',可选=' + unselectedCount);

  if (availableSeats.length === 0) {
    debugInfo.push('❌ 该时段无可用座位');
    finish({ status: 'ROW_OR_BUTTON_NOT_FOUND', before: before, after: before, info: debugInfo.join(' | ') });
    return;
  }

  // 7. 选择目标座位（courtIndex 从1开始）
  var targetSeatIndex = Math.min(availableSeats.length, courtIndex) - 1;
  var targetSeat = availableSeats[targetSeatIndex];

  debugInfo.push('🎯 选择第' + courtIndex + '个可用座位(索引:' + targetSeatIndex + ')');

  // 8. 点击座位
  try {
    targetSeat.scrollIntoView({ block: 'center' });
    targetSeat.click();
    debugInfo.push('✓ 已点击座位');
  } catch (err) {
    try {
      // 尝试点击内部元素
      var innerSeat = targetSeat.querySelector('.inner-seat');
      if (innerSeat) {
        innerSeat.click();
        debugInfo.push('✓ 已点击座位(inner)');
      }
    } catch (err2) {
      debugInfo.push('❌ 点击失败:' + err2);
    }
  }

  // 9. 等待状态变化并检查结果
  window.setTimeout(function () {
    var after = getPanelState();
    var changed = (after.selectedCount > before.selectedCount) ||
      (after.amount > before.amount) ||
      (!before.submitEnabled && after.submitEnabled);

    debugInfo.push('📈 状态: 选中' + before.selectedCount + '->' + after.selectedCount +
                   ', 金额' + before.amount + '->' + after.amount);

    finish({
      status: changed ? 'OK_SELECTED' : 'CLICK_NO_EFFECT',
      before: before,
      after: after,
      info: debugInfo.join(' | ')
    });
  }, 600);
} catch (err) {
  var info = '';
  try {
    info = err && (err.stack || err.message || String(err));
  } catch (_) {
    info = String(err);
  }
  finish({ status: 'JS_EXCEPTION', before: null, after: null, info: info });
}
"""

def strict_select_slot(driver, time_text, court_index, config=None):
    """用 JS 执行严格选择；返回 True 表示"选择产生了实际效果" """
    try:
        raw = driver.execute_async_script(STRICT_CHECK_JS, time_text, court_index)
        if isinstance(raw, str):
            try:
                res = json.loads(raw)
            except Exception as exc:
                log(f"JS 返回内容无法解析：{raw!r} ({exc})")
                if config and config.debug:
                    _save_debug_info(driver, f"parse_error_{time_text}_{court_index}")
                return False
        elif isinstance(raw, dict):
            res = raw
        else:
            log(f"JS 返回异常：{raw!r}")
            if config and config.debug:
                _save_debug_info(driver, f"js_error_{time_text}_{court_index}")
            return False
        status = res.get("status")
        log(
            "时间{} 第{}块 -> {} | before={} after={} info={}".format(
                time_text,
                court_index,
                status,
                res.get("before"),
                res.get("after"),
                res.get("info"),
            )
        )

        # 如果失败且启用了调试模式，保存调试信息
        if status != "OK_SELECTED" and config and config.debug:
            _save_debug_info(driver, f"{status}_{time_text}_{court_index}")

        return status == "OK_SELECTED"
    except Exception as e:
        log(f"JS 执行失败：{e}")
        if config and config.debug:
            _save_debug_info(driver, f"exception_{time_text}_{court_index}")
        return False


def _save_debug_info(driver, suffix):
    """保存页面截图和 HTML 用于调试"""
    try:
        timestamp = datetime.now(TZ).strftime("%Y%m%d_%H%M%S")
        filename = f"debug_{timestamp}_{suffix}"

        # 保存截图
        screenshot_path = f"{filename}.png"
        driver.save_screenshot(screenshot_path)
        log(f"📸 已保存截图：{screenshot_path}")

        # 保存页面 HTML
        html_path = f"{filename}.html"
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(driver.page_source)
        log(f"💾 已保存 HTML：{html_path}")
    except Exception as e:
        log(f"⚠ 保存调试信息失败：{e}")


def click_selected_court_icon(driver, time_text: str, court_index: int, timeout: float = 2.5) -> bool:
    """在右侧订单面板中点击已选场地的图标/卡片，返回是否点击成功。"""
    keywords = [
        time_text,
        f"第{court_index}",
        f"{court_index}号",
        f"{court_index}块",
        f"场地{court_index}",
        f"球场{court_index}",
    ]
    container_xpath = "//aside | //div[contains(@class,'order')] | //div[contains(@class,'right')] | //div[contains(@class,'detail')]"
    end_time = time.time() + timeout
    while time.time() < end_time:
        try:
            containers = driver.find_elements(By.XPATH, container_xpath)
        except Exception:
            containers = []
        for container in containers:
            try:
                if not container.is_displayed():
                    continue
            except Exception:
                pass
            for key in keywords:
                if not key:
                    continue
                try:
                    el = container.find_element(
                        By.XPATH, f".//*[contains(normalize-space(), '{key}')]"
                    )
                except Exception:
                    continue

                candidate = el
                for _ in range(4):
                    if candidate is None:
                        break
                    try:
                        tag = candidate.tag_name.lower()
                    except Exception:
                        tag = ""
                    try:
                        displayed = candidate.is_displayed()
                    except Exception:
                        displayed = True
                    if tag in {"button", "a", "li", "div", "span"} and displayed:
                        try:
                            driver.execute_script(
                                "arguments[0].scrollIntoView({block:'center'});", candidate
                            )
                        except Exception:
                            pass
                        for action in (candidate.click, lambda e=candidate: driver.execute_script("arguments[0].click();", e)):
                            try:
                                action()
                                log(f"已点击已选场地图标：{key}")
                                return True
                            except Exception:
                                continue
                    try:
                        candidate = candidate.find_element(By.XPATH, "..")
                    except Exception:
                        candidate = None
                # 若找到关键字但未成功点击，尝试下一个关键字
        time.sleep(0.2)
    log("❌ 未能点击已选场地图标，请检查右侧订单信息区域。")
    return False


def click_submit_order_button(driver, timeout: float = 3.0) -> bool:
    """点击“立即下单/提交订单”等按钮。"""
    labels = ["立即下单", "提交订单", "立即预约", "确认预约", "立即支付"]
    end_time = time.time() + timeout
    while time.time() < end_time:
        for label in labels:
            try:
                buttons = driver.find_elements(
                    By.XPATH, f"//button[contains(normalize-space(), '{label}')]"
                )
            except Exception:
                buttons = []
            for btn in buttons:
                try:
                    if not btn.is_displayed():
                        continue
                except Exception:
                    pass
                try:
                    driver.execute_script(
                        "arguments[0].scrollIntoView({block:'center'});", btn
                    )
                except Exception:
                    pass
                for action in (btn.click, lambda b=btn: driver.execute_script("arguments[0].click();", b)):
                    try:
                        action()
                        log(f"已点击下单按钮：{label}")
                        return True
                    except Exception:
                        continue
        time.sleep(0.2)
    log("❌ 未能点击立即下单按钮。")
    return False


def accept_booking_notice_if_present(driver, timeout: float = 4.0) -> bool:
    """若弹出“预订须知”提示框，则自动勾选并提交。"""
    end_time = time.time() + timeout
    while time.time() < end_time:
        try:
            dialog = driver.find_element(
                By.XPATH,
                "//div[contains(@class, 'el-dialog') and .//span[contains(normalize-space(), '预订须知')]]",
            )
        except Exception:
            # 未找到弹窗，视为无需处理
            return True

        try:
            if not dialog.is_displayed():
                time.sleep(0.1)
                continue
        except Exception:
            time.sleep(0.1)
            continue

        try:
            checkbox_input = dialog.find_element(
                By.XPATH,
                ".//label[contains(@class, 'el-checkbox')]//input[@type='checkbox']",
            )
            if not checkbox_input.is_selected():
                try:
                    checkbox = dialog.find_element(
                        By.XPATH,
                        ".//label[contains(@class, 'el-checkbox')]//span[contains(@class, 'el-checkbox__inner')]",
                    )
                    driver.execute_script("arguments[0].click();", checkbox)
                except Exception:
                    driver.execute_script("arguments[0].click();", checkbox_input)
            log("已勾选预订须知复选框。")
        except Exception:
            log("⚠ 未能自动勾选预订须知复选框。")
            return False

        try:
            submit_btn = dialog.find_element(
                By.XPATH,
                ".//button[.//span[contains(normalize-space(), '提交订单')]]",
            )
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", submit_btn)
            for action in (
                submit_btn.click,
                lambda b=submit_btn: driver.execute_script("arguments[0].click();", b),
            ):
                try:
                    action()
                    log("已在预订须知弹窗内点击提交订单。")
                    return True
                except Exception:
                    continue
        except Exception:
            log("⚠ 未能在预订须知弹窗内点击提交订单。")
            return False

    log("⚠ 处理预订须知弹窗超时。")
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
            log(f"尝试预约组合：时间 {t}，第{c}块场地")
            for _ in range(config.click_retries):
                if strict_select_slot(driver, t, c, config):
                    ok = True
                    break
                time.sleep(0.05)
            if not ok:
                continue

            if not click_selected_court_icon(driver, t, c):
                log("⚠ 未能激活右侧的已选场地，尝试下一组合。")
                continue

            if not click_submit_order_button(driver):
                log("⚠ 未能点击立即下单按钮，尝试下一组合。")
                continue

            if not accept_booking_notice_if_present(driver):
                log("⚠ 未能通过预订须知弹窗，尝试下一组合。")
                continue

            time.sleep(0.2)
            confirm_if_needed(driver)

            if wait_success_toast(driver, timeout=6):
                log(f"✅ {c}号场地{t}时间预约成功")
                return
            else:
                log(f"⚠ {c}号场地{t}时间预约失败，继续尝试下一组合。")

    log("❌ 全部时间与场地组合尝试完毕，未成功。")

def parse_args(argv):
    default_config = BookingConfig().with_defaults()
    parser = argparse.ArgumentParser(
        description="SJTU 体育馆自动预约脚本（需手动登录后使用）"
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
        "--venue-name",
        default=default_config.venue_name,
        help="目标场馆名称 (默认: %(default)s)",
    )
    parser.add_argument(
        "--activity-name",
        default=default_config.activity_name,
        help="目标活动/项目名称 (默认: %(default)s)",
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
    parser.add_argument(
        "--debug",
        action="store_true",
        default=default_config.debug,
        help="启用调试模式，失败时保存截图和 HTML",
    )

    args = parser.parse_args(argv)
    config = BookingConfig(
        start_url=args.start_url,
        date_offset_days=args.date_offset,
        open_time_str=args.open_time,
        venue_name=args.venue_name,
        activity_name=args.activity_name,
        preferred_slots=_split_list(args.slots),
        preferred_courts=_split_list(args.courts, type_=int),
        click_retries=args.click_retries,
        headless=args.headless,
        debug=args.debug,
    ).with_defaults()
    return args, config


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    args, config = parse_args(argv)
    driver = build_driver(config)
    try:
        driver.get(config.start_url)
        try:
            log(f"初始页面 URL：{driver.current_url}")
        except Exception:
            pass
        log("请手动登录体育场馆系统，完成后按 Enter 继续（脚本会在指定时间自动刷新并前往目标场馆/活动）。")
        input("登录完成后按 Enter ... ")

        if args.now:
            if not navigate_to_activity(driver, config, force_home=True):
                log("❌ 无法自动跳转至目标场馆/活动，结束任务。")
                return
            log("立即执行 (--now)")
            booking_flow(driver, config)
        else:
            target_dt = next_open_time(config.open_time_str)
            wait_until(target_dt)
            log("⏰ 已到开放时间，刷新页面以获取最新数据。")
            driver.refresh()
            wait_document_ready(driver, timeout=12)
            try:
                log(f"刷新后当前 URL：{driver.current_url}")
            except Exception:
                pass
            log("🔄 页面刷新完成，尝试自动跳转至目标场馆和活动。")
            if not navigate_to_activity(driver, config, force_home=False):
                log("ℹ️ 当前页面未能直接进入目标场馆，尝试重新打开主页面。")
                if not navigate_to_activity(driver, config, force_home=True):
                    log("❌ 刷新后自动导航失败，结束任务。")
                    return
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
