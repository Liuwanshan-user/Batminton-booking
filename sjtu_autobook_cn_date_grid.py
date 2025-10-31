#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
SJTU Sports Auto-Booker — 智能场地扫描版本
---------------------------------------------------------------------------
使用方式：
1) 先手动登录 https://sports.sjtu.edu.cn/ 并进入预约页面
2) 运行脚本（可用 --help 查看完整参数）：
   - 立刻测试：python this_script.py --now
   - 指定日期偏移/时间段：python this_script.py --date-offset 7 --slots "18:00,19:00,20:00"
   - 等到指定时间：python this_script.py --open-time "12:00:00"
3) 脚本会在目标时间自动：
   - 根据配置导航到指定场馆/活动（默认：南区体育馆 → 乒乓球）
   - 选择"今天 + DATE_OFFSET_DAYS"的日期页签
   - 对于每个时间段，自动扫描并选择第一个可用场地
   - 自动提交订单并处理预订须知对话框
   - 检测跳转到支付页面表示预订成功

核心特性：
- 🎯 智能场地扫描：对每个时间段自动查找第一个可用场地，无需手动指定
- ⚡ 快速跳过：通过JS快速检查时间段可用性，跳过已满时段
- 🔄 自动重试：失败后自动刷新并从断点继续
- 📊 详细日志：实时显示扫描进度和结果

注意：
- 不处理 jAccount 验证码；需手动登录
- 场地选择策略：按顺序扫描，找到第一个可用就立即预订
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
    open_time_str: str = "19:09:59"
    venue_name: str = "南区体育馆"
    activity_name: str = "乒乓球"
    preferred_slots: List[str] = None
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
                "21:00",
                "12:00",
                "13:00",
                "14:00",
                "15:00",
                "16:00",
                "17:00",
            ]),
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


def wait_document_ready(driver, timeout=3):
    try:
        WebDriverWait(driver, timeout).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        return True
    except TimeoutException:
        log("⚠ 页面在指定时间内未完全加载。")
        return False


def smart_wait_for_element(driver, by, value, timeout=2, condition="clickable"):
    """智能等待元素出现，支持多种条件"""
    try:
        if condition == "clickable":
            element = WebDriverWait(driver, timeout).until(
                EC.element_to_be_clickable((by, value))
            )
        elif condition == "visible":
            element = WebDriverWait(driver, timeout).until(
                EC.visibility_of_element_located((by, value))
            )
        elif condition == "present":
            element = WebDriverWait(driver, timeout).until(
                EC.presence_of_element_located((by, value))
            )
        return element
    except TimeoutException:
        return None


def smart_wait_for_any_condition(driver, conditions, timeout=2):
    """等待多个条件中的任意一个满足"""
    end_time = time.time() + timeout
    while time.time() < end_time:
        for condition_func in conditions:
            try:
                result = condition_func()
                if result:
                    return result
            except Exception:
                pass
        time.sleep(0.05)
    return None


def is_on_grid_page(driver) -> bool:
    try:
        body_text = driver.find_element(By.TAG_NAME, "body").text
    except Exception:
        body_text = driver.page_source
    keywords = ["选中的", "立即下单", "预约", "金额"]
    return any(k in body_text for k in keywords)


def wait_for_grid_ready(driver, timeout=3):
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


def _click_element_by_text(driver, target_text: str, description: str, timeout=3):
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
                time.sleep(0.03)  # 优化：减少延迟
                return True
        time.sleep(0.03)  # 优化：减少延迟
    log(f"❌ 未能点击{description}：{target_text}")
    return False


def _click_card_heading(driver, heading_text: str, description: str, timeout=3):
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
        search_input = WebDriverWait(driver, 2).until(
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
        time.sleep(0.1)  # 优化：减少延迟
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
        time.sleep(0.05)  # 优化：减少延迟
        if _switch_to_new_window(driver, handles_before):
            wait_document_ready(driver, timeout=2)  # 优化：减少超时时间
        if config.activity_name and not _click_element_by_text(driver, config.activity_name, "活动"):
            return False
        wait_for_grid_ready(driver, timeout=3)
        return is_on_grid_page(driver)

    if force_home:
        log(f"打开预约主页面：{config.start_url}")
        driver.get(config.start_url)
        wait_document_ready(driver, timeout=3)
        try:
            driver.switch_to.window(driver.window_handles[-1])
        except Exception:
            pass

    if _navigate_from_current_page():
        log(f"✅ 已导航到目标页面：{config.venue_name} → {config.activity_name}")
        return True

    log("当前页面未找到目标场馆/活动，尝试返回主页面重新定位。")
    driver.get(config.start_url)
    wait_document_ready(driver, timeout=3)
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
    d.implicitly_wait(0.5)
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
    for _ in range(5):
        for txt in candidates:
            try:
                el = WebDriverWait(driver, 0.3).until(
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

# ---------- 查找第一个可用场地 ----------
FIND_FIRST_AVAILABLE_COURT_JS = r"""
/*
  查找指定时间段内第一个可用的场地
  使用直接索引映射：不读取文字，直接根据时间计算行索引
  包含等待逻辑：等待座位行加载完成

  索引映射规则：
  - 时间格式: "07:00", "08:00", ..., "21:00"
  - 行索引: 07:00 -> 0, 08:00 -> 1, ..., 18:00 -> 11, ..., 21:00 -> 14
  - 计算公式: rowIndex = hour - 7

  返回: {
    found: true/false,
    courtIndex: 场地编号(1-based),
    courtName: 场地名称,
    totalSeats: 总座位数,
    debugInfo: 调试信息
  }
*/
var timeText = arguments[0];
var debug = arguments[1] || false;
var done = arguments[arguments.length - 1];

function norm(s) { return String(s || '').trim().replace(/\s+/g, ''); }
function isVisible(el) { return !!(el && el.offsetParent !== null); }

// 等待并获取座位行的函数
function getSeatRows(wrapper) {
  var seatRows = [];
  var wrapperChildren = Array.from(wrapper.children);

  for (var i = 0; i < wrapperChildren.length; i++) {
    var child = wrapperChildren[i];
    if (child.tagName.toLowerCase() === 'ul') continue;
    if (child.tagName.toLowerCase() === 'div' && child.classList.contains('clearfix')) {
      seatRows.push(child);
    }
  }

  return seatRows;
}

try {
  var debugInfo = [];
  debugInfo.push('🎯 目标时间:' + timeText);

  // 步骤1: 解析时间字符串，直接计算行索引
  var hour = -1;
  try {
    var parts = timeText.split(':');
    if (parts.length >= 1) {
      hour = parseInt(parts[0], 10);
    }
  } catch (err) {
    done({ found: false, courtIndex: 0, courtName: '', totalSeats: 0, debugInfo: '❌ 时间格式错误:' + timeText });
    return;
  }

  if (hour < 7 || hour > 21) {
    done({ found: false, courtIndex: 0, courtName: '', totalSeats: 0, debugInfo: '❌ 时间超出范围(7-21):' + hour });
    return;
  }

  // 直接计算行索引：7点=0, 8点=1, ..., 18点=11, ..., 21点=14
  var rowIndex = hour - 7;
  debugInfo.push('✓ 时间' + timeText + '→行索引' + rowIndex + ' (计算:' + hour + '-7)');

  // 步骤2: 获取右侧座位容器
  var wrapper = document.querySelector('div.inner-seat-wrapper.clearfix');
  if (!wrapper) {
    done({ found: false, courtIndex: 0, courtName: '', totalSeats: 0, debugInfo: debugInfo.join(' | ') + ' | ❌ 未找到wrapper' });
    return;
  }

  // 步骤3: 等待座位行加载（最多等待2秒）
  var maxWaitTime = 2000; // 2秒
  var checkInterval = 100; // 每100ms检查一次
  var startTime = Date.now();
  var attemptCount = 0;

  var checkSeatRows = function() {
    attemptCount++;
    var seatRows = getSeatRows(wrapper);

    if (seatRows.length > 0) {
      // 找到座位行了！
      debugInfo.push('座位行数:' + seatRows.length + '行 (等待' + attemptCount + '次,' + (Date.now() - startTime) + 'ms)');

      if (rowIndex >= seatRows.length) {
        done({ found: false, courtIndex: 0, courtName: '', totalSeats: 0, debugInfo: debugInfo.join(' | ') + ' | ❌ 行索引' + rowIndex + '超出范围(共' + seatRows.length + '行)' });
        return;
      }

      // 步骤4: 直接访问对应行的所有座位
      var targetRow = seatRows[rowIndex];
      var allSeats = Array.from(targetRow.children).filter(function(child) {
        return child.classList.contains('seat');
      });

      debugInfo.push('目标行座位数:' + allSeats.length);

      // 步骤5: 查找第一个可用场地
      for (var j = 0; j < allSeats.length; j++) {
        var seat = allSeats[j];
        var innerSeat = seat.querySelector('.inner-seat');
        if (!innerSeat) continue;

        var innerClass = innerSeat.className || '';
        var hasUnselectedSeat = innerClass.indexOf('unselected-seat') !== -1;
        var hasBoughtSeat = innerClass.indexOf('bought-seat') !== -1;

        if (hasUnselectedSeat && !hasBoughtSeat && isVisible(seat)) {
          // 找到第一个可用场地
          var courtIndex = j + 1; // 1-based

          // 尝试获取场地名称
          var courtName = '场地' + courtIndex;
          try {
            var topUl = wrapper.querySelector('ul[style*="position"]');
            if (topUl) {
              var courtItems = Array.from(topUl.querySelectorAll('li'));
              if (j < courtItems.length) {
                courtName = norm(courtItems[j].textContent) || courtName;
              }
            }
          } catch (err) {}

          debugInfo.push('✓ 找到第一个可用场地:' + courtName + ' (索引' + courtIndex + ')');

          done({
            found: true,
            courtIndex: courtIndex,
            courtName: courtName,
            totalSeats: allSeats.length,
            debugInfo: debugInfo.join(' | ')
          });
          return;
        }
      }

      // 未找到可用场地
      debugInfo.push('❌ 该时间段无可用场地');
      done({
        found: false,
        courtIndex: 0,
        courtName: '',
        totalSeats: allSeats.length,
        debugInfo: debugInfo.join(' | ')
      });
      return;
    }

    // 还没找到座位行，检查是否超时
    if (Date.now() - startTime >= maxWaitTime) {
      // 超时了，输出调试信息
      var wrapperChildren = Array.from(wrapper.children);
      var childTags = [];
      for (var i = 0; i < Math.min(wrapperChildren.length, 10); i++) {
        var c = wrapperChildren[i];
        var tag = c.tagName.toLowerCase();
        var cls = c.className || 'no-class';
        childTags.push(tag + '.' + cls);
      }
      debugInfo.push('等待超时(' + maxWaitTime + 'ms,' + attemptCount + '次)');
      debugInfo.push('Wrapper子元素:' + childTags.join(','));
      done({ found: false, courtIndex: 0, courtName: '', totalSeats: 0, debugInfo: debugInfo.join(' | ') + ' | ❌ 未找到座位行' });
      return;
    }

    // 继续等待
    setTimeout(checkSeatRows, checkInterval);
  };

  // 开始检查
  checkSeatRows();

} catch (err) {
  done({
    found: false,
    courtIndex: 0,
    courtName: '',
    totalSeats: 0,
    debugInfo: 'JS异常: ' + (err.message || String(err))
  });
}
"""

# ---------- 快速检查时间段可用场地数量 ----------
QUICK_CHECK_AVAILABILITY_JS = r"""
/*
  快速检查指定时间段的可用场地数量
  使用直接索引映射：不读取文字，直接根据时间计算行索引
  包含等待逻辑：等待座位行加载完成

  索引映射规则：
  - 时间格式: "07:00", "08:00", ..., "21:00"
  - 行索引: 07:00 -> 0, 08:00 -> 1, ..., 18:00 -> 11, ..., 21:00 -> 14
  - 计算公式: rowIndex = hour - 7
*/
var timeText = arguments[0];
var debug = arguments[1] || false;
var done = arguments[arguments.length - 1];

function norm(s) { return String(s || '').trim().replace(/\s+/g, ''); }
function isVisible(el) { return !!(el && el.offsetParent !== null); }

// 等待并获取座位行的函数
function getSeatRows(wrapper) {
  var seatRows = [];
  var wrapperChildren = Array.from(wrapper.children);

  for (var i = 0; i < wrapperChildren.length; i++) {
    var child = wrapperChildren[i];
    if (child.tagName.toLowerCase() === 'ul') continue;
    if (child.tagName.toLowerCase() === 'div' && child.classList.contains('clearfix')) {
      seatRows.push(child);
    }
  }

  return seatRows;
}

try {
  var debugInfo = [];
  var sampleClasses = [];
  debugInfo.push('🎯 目标时间:' + timeText);

  // 步骤1: 解析时间字符串，直接计算行索引
  var hour = -1;
  try {
    var parts = timeText.split(':');
    if (parts.length >= 1) {
      hour = parseInt(parts[0], 10);
    }
  } catch (err) {
    debugInfo.push('❌ 时间格式错误:' + timeText);
    done({ timeFound: false, availableCount: 0, totalSeats: 0, boughtCount: 0, debugInfo: debugInfo.join(' | '), sampleClasses: [] });
    return;
  }

  if (hour < 7 || hour > 21) {
    debugInfo.push('❌ 时间超出范围(7-21):' + hour);
    done({ timeFound: false, availableCount: 0, totalSeats: 0, boughtCount: 0, debugInfo: debugInfo.join(' | '), sampleClasses: [] });
    return;
  }

  // 直接计算行索引：7点=0, 8点=1, ..., 18点=11, ..., 21点=14
  var rowIndex = hour - 7;
  debugInfo.push('✓ 时间' + timeText + '→行索引' + rowIndex + ' (计算:' + hour + '-7)');

  // 步骤2: 获取右侧座位容器
  var wrapper = document.querySelector('div.inner-seat-wrapper.clearfix');
  if (!wrapper) {
    debugInfo.push('❌ 未找到wrapper');
    done({ timeFound: false, availableCount: 0, totalSeats: 0, boughtCount: 0, debugInfo: debugInfo.join(' | '), sampleClasses: [] });
    return;
  }

  // 步骤3: 等待座位行加载（最多等待2秒）
  var maxWaitTime = 2000; // 2秒
  var checkInterval = 100; // 每100ms检查一次
  var startTime = Date.now();
  var attemptCount = 0;

  var checkSeatRows = function() {
    attemptCount++;
    var seatRows = getSeatRows(wrapper);

    if (seatRows.length > 0) {
      // 找到座位行了！
      debugInfo.push('座位行数:' + seatRows.length + '行 (等待' + attemptCount + '次,' + (Date.now() - startTime) + 'ms)');

      // 输出每行的座位数量用于调试
      if (debug) {
        var rowInfo = [];
        for (var i = 0; i < Math.min(seatRows.length, 20); i++) {
          var rowSeats = Array.from(seatRows[i].children).filter(function(child) {
            return child.classList.contains('seat');
          });
          // 统计该行的状态
          var bCount = 0, uCount = 0;
          for (var j = 0; j < rowSeats.length; j++) {
            var innerSeat = rowSeats[j].querySelector('.inner-seat');
            if (innerSeat) {
              var cls = innerSeat.className || '';
              if (cls.indexOf('bought-seat') !== -1) bCount++;
              else if (cls.indexOf('unselected-seat') !== -1) uCount++;
            }
          }
          rowInfo.push(i + ':' + rowSeats.length + '座(' + uCount + '可用,' + bCount + '已订)');
        }
        debugInfo.push('座位行明细=' + rowInfo.join(','));
      }

      if (rowIndex >= seatRows.length) {
        debugInfo.push('❌ 行索引' + rowIndex + '超出范围(共' + seatRows.length + '行)');
        done({ timeFound: true, availableCount: 0, totalSeats: 0, boughtCount: 0, debugInfo: debugInfo.join(' | '), sampleClasses: [] });
        return;
      }

      // 步骤4: 直接访问对应行的所有座位
      var targetRow = seatRows[rowIndex];
      var allSeats = Array.from(targetRow.children).filter(function(child) {
        return child.classList.contains('seat');
      });

      debugInfo.push('目标行座位数:' + allSeats.length);

      var availableCount = 0;
      var boughtCount = 0;

      // 步骤5: 遍历该行所有座位，统计可用和已订数量
      for (var j = 0; j < allSeats.length; j++) {
        var seat = allSeats[j];
        var innerSeat = seat.querySelector('.inner-seat');
        if (!innerSeat) continue;

        var innerClass = innerSeat.className || '';

        // 收集前3个座位的class作为样本（用于调试）
        if (sampleClasses.length < 3) {
          sampleClasses.push(innerClass);
        }

        // 精确匹配：bought-seat = 已订，unselected-seat = 可选
        var hasBoughtSeat = innerClass.indexOf('bought-seat') !== -1;
        var hasUnselectedSeat = innerClass.indexOf('unselected-seat') !== -1;

        if (hasBoughtSeat) {
          boughtCount++;
        } else if (hasUnselectedSeat && isVisible(seat)) {
          availableCount++;
        }
      }

      debugInfo.push('统计结果: 可用=' + availableCount + ' 已订=' + boughtCount + ' 总计=' + allSeats.length);

      done({
        timeFound: true,
        availableCount: availableCount,
        totalSeats: allSeats.length,
        boughtCount: boughtCount,
        debugInfo: debugInfo.join(' | '),
        sampleClasses: sampleClasses
      });
      return;
    }

    // 还没找到座位行，检查是否超时
    if (Date.now() - startTime >= maxWaitTime) {
      // 超时了，输出调试信息
      var wrapperChildren = Array.from(wrapper.children);
      var childTags = [];
      for (var i = 0; i < Math.min(wrapperChildren.length, 10); i++) {
        var c = wrapperChildren[i];
        var tag = c.tagName.toLowerCase();
        var cls = c.className || 'no-class';
        childTags.push(tag + '.' + cls);
      }
      debugInfo.push('等待超时(' + maxWaitTime + 'ms,' + attemptCount + '次)');
      debugInfo.push('Wrapper子元素:' + childTags.join(','));
      done({ timeFound: false, availableCount: 0, totalSeats: 0, boughtCount: 0, debugInfo: debugInfo.join(' | '), sampleClasses: [] });
      return;
    }

    // 继续等待
    setTimeout(checkSeatRows, checkInterval);
  };

  // 开始检查
  checkSeatRows();

} catch (err) {
  done({
    timeFound: false,
    availableCount: 0,
    totalSeats: 0,
    boughtCount: 0,
    debugInfo: 'JS异常: ' + (err.message || String(err)),
    sampleClasses: []
  });
}
"""

# ---------- 关键：严格判断是否"真的选择成功/已下单成功" ----------
STRICT_CHECK_JS = r"""
/*
  使用直接索引映射：不读取文字，直接根据时间计算行索引
  包含等待逻辑：等待座位行加载完成

  索引映射规则：
  - 时间格式: "07:00", "08:00", ..., "21:00"
  - 行索引: 07:00 -> 0, 08:00 -> 1, ..., 18:00 -> 11, ..., 21:00 -> 14
  - 计算公式: rowIndex = hour - 7
  - 场地索引: courtIndex (1-based) -> array[courtIndex - 1]

  返回：{
    status: "OK_SELECTED" | "TIME_NOT_FOUND" | "COURT_NOT_AVAILABLE" | "CLICK_NO_EFFECT" | "JS_EXCEPTION",
    before: { selectedCount, amount, submitEnabled },
    after:  { selectedCount, amount, submitEnabled },
    info: "详细信息"
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

// 等待并获取座位行的函数
function getSeatRows(wrapper) {
  var seatRows = [];
  var wrapperChildren = Array.from(wrapper.children);

  for (var i = 0; i < wrapperChildren.length; i++) {
    var child = wrapperChildren[i];
    if (child.tagName.toLowerCase() === 'ul') continue;
    if (child.tagName.toLowerCase() === 'div' && child.classList.contains('clearfix')) {
      seatRows.push(child);
    }
  }

  return seatRows;
}

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
  var debugInfo = [];
  debugInfo.push('🎯 目标:时间=' + timeText + ' 场地=' + courtIndex);

  // ========== 步骤1: 解析时间字符串，直接计算行索引 ==========
  var hour = -1;
  try {
    var parts = timeText.split(':');
    if (parts.length >= 1) {
      hour = parseInt(parts[0], 10);
    }
  } catch (err) {
    debugInfo.push('❌ 时间格式错误:' + timeText);
    finish({ status: 'TIME_NOT_FOUND', before: before, after: before, info: debugInfo.join(' | ') });
    return;
  }

  if (hour < 7 || hour > 21) {
    debugInfo.push('❌ 时间超出范围(7-21):' + hour);
    finish({ status: 'TIME_NOT_FOUND', before: before, after: before, info: debugInfo.join(' | ') });
    return;
  }

  // 直接计算行索引：7点=0, 8点=1, ..., 18点=11, ..., 21点=14
  var rowIndex = hour - 7;
  debugInfo.push('✓ 时间' + timeText + '→行索引' + rowIndex + ' (计算:' + hour + '-7)');

  // ========== 步骤2: 获取右侧座位容器 ==========
  var wrapper = document.querySelector('div.inner-seat-wrapper.clearfix');
  if (!wrapper) {
    debugInfo.push('❌ 未找到wrapper');
    finish({ status: 'COURT_NOT_AVAILABLE', before: before, after: before, info: debugInfo.join(' | ') });
    return;
  }

  // ========== 步骤3: 等待座位行加载（最多等待2秒） ==========
  var maxWaitTime = 2000; // 2秒
  var checkInterval = 100; // 每100ms检查一次
  var startTime = Date.now();
  var attemptCount = 0;

  var processSeatSelection = function() {
    attemptCount++;
    var seatRows = getSeatRows(wrapper);

    if (seatRows.length > 0) {
      // 找到座位行了！
      debugInfo.push('座位行数:' + seatRows.length + '行 (等待' + attemptCount + '次,' + (Date.now() - startTime) + 'ms)');

      if (rowIndex >= seatRows.length) {
        debugInfo.push('❌ 行索引' + rowIndex + '超出范围(共' + seatRows.length + '行)');
        finish({ status: 'COURT_NOT_AVAILABLE', before: before, after: before, info: debugInfo.join(' | ') });
        return;
      }

      // 步骤4: 直接访问对应行的所有座位
      var targetRow = seatRows[rowIndex];
      var allSeats = Array.from(targetRow.children).filter(function(child) {
        return child.classList.contains('seat');
      });

      debugInfo.push('目标行座位数:' + allSeats.length);

      if (courtIndex > allSeats.length) {
        debugInfo.push('❌ 场地' + courtIndex + '超出范围(共' + allSeats.length + '个)');
        finish({ status: 'COURT_NOT_AVAILABLE', before: before, after: before, info: debugInfo.join(' | ') });
        return;
      }

      // 步骤5: 选择指定场地（场地编号从1开始，数组索引从0开始）
      var targetSeat = allSeats[courtIndex - 1];
      var innerSeat = targetSeat.querySelector('.inner-seat');

      if (!innerSeat) {
        debugInfo.push('❌ 场地' + courtIndex + '没有inner-seat');
        finish({ status: 'COURT_NOT_AVAILABLE', before: before, after: before, info: debugInfo.join(' | ') });
        return;
      }

      var innerClass = innerSeat.className || '';
      debugInfo.push('场地' + courtIndex + ':' + innerClass);

      // 检查是否可用
      var hasBoughtSeat = innerClass.indexOf('bought-seat') !== -1;
      var hasUnselectedSeat = innerClass.indexOf('unselected-seat') !== -1;

      if (hasBoughtSeat) {
        debugInfo.push('❌ 场地' + courtIndex + '已被预订(bought-seat)');
        finish({ status: 'COURT_NOT_AVAILABLE', before: before, after: before, info: debugInfo.join(' | ') });
        return;
      }

      if (!hasUnselectedSeat) {
        debugInfo.push('❌ 场地' + courtIndex + '不可选(无unselected-seat)');
        finish({ status: 'COURT_NOT_AVAILABLE', before: before, after: before, info: debugInfo.join(' | ') });
        return;
      }

      debugInfo.push('✓ 场地' + courtIndex + '可用(unselected-seat)');

      // 步骤6: 点击场地
      try {
        targetSeat.scrollIntoView({ block: 'center' });
      } catch (err) {
        debugInfo.push('⚠️ scrollIntoView失败');
      }

      var clickSuccess = false;
      var clickMethods = [
        function() { innerSeat.click(); return 'inner-seat.click()'; },
        function() { targetSeat.click(); return 'seat.click()'; },
        function() {
          var evt = new MouseEvent('click', { bubbles: true, cancelable: true, view: window });
          innerSeat.dispatchEvent(evt);
          return 'innerSeat.dispatchEvent';
        }
      ];

      for (var m = 0; m < clickMethods.length; m++) {
        try {
          var methodName = clickMethods[m]();
          debugInfo.push('🖱️ ' + methodName);
          clickSuccess = true;
          break;
        } catch (err) {
          debugInfo.push('⚠️ 方法' + (m+1) + '失败');
        }
      }

      if (!clickSuccess) {
        debugInfo.push('❌ 所有点击方法都失败');
        finish({ status: 'CLICK_NO_EFFECT', before: before, after: before, info: debugInfo.join(' | ') });
        return;
      }

      // 步骤7: 检测右侧面板状态变化
      var checkCount = 0;
      var maxChecks = 10;
      var checkInterval2 = 50;

      var intervalId = window.setInterval(function () {
        checkCount++;
        var after = getPanelState();
        var changed = (after.selectedCount > before.selectedCount) ||
          (after.amount > before.amount) ||
          (!before.submitEnabled && after.submitEnabled);

        if (changed || checkCount >= maxChecks) {
          window.clearInterval(intervalId);

          debugInfo.push('状态:选中' + before.selectedCount + '→' + after.selectedCount +
                         ' 金额￥' + before.amount + '→￥' + after.amount +
                         ' 下单按钮:' + (before.submitEnabled ? '可用' : '不可用') + '→' + (after.submitEnabled ? '可用' : '不可用') +
                         ' (轮询' + checkCount + '次)');

          finish({
            status: changed ? 'OK_SELECTED' : 'CLICK_NO_EFFECT',
            before: before,
            after: after,
            info: debugInfo.join(' | ')
          });
        }
      }, checkInterval2);
      return;
    }

    // 还没找到座位行，检查是否超时
    if (Date.now() - startTime >= maxWaitTime) {
      // 超时了，输出调试信息
      var wrapperChildren = Array.from(wrapper.children);
      var childTags = [];
      for (var i = 0; i < Math.min(wrapperChildren.length, 10); i++) {
        var c = wrapperChildren[i];
        var tag = c.tagName.toLowerCase();
        var cls = c.className || 'no-class';
        childTags.push(tag + '.' + cls);
      }
      debugInfo.push('等待超时(' + maxWaitTime + 'ms,' + attemptCount + '次)');
      debugInfo.push('Wrapper子元素:' + childTags.join(','));
      finish({ status: 'COURT_NOT_AVAILABLE', before: before, after: before, info: debugInfo.join(' | ') });
      return;
    }

    // 继续等待
    setTimeout(processSeatSelection, checkInterval);
  };

  // 开始处理
  processSeatSelection();

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

def find_first_available_court(driver, time_text, debug=False):
    """
    查找指定时间段内第一个可用的场地
    返回: (found, court_index, court_name, total_seats, debug_info)
    """
    try:
        result = driver.execute_async_script(FIND_FIRST_AVAILABLE_COURT_JS, time_text, debug)
        if isinstance(result, dict):
            found = result.get("found", False)
            court_index = result.get("courtIndex", 0)
            court_name = result.get("courtName", "")
            total_seats = result.get("totalSeats", 0)
            debug_info = result.get("debugInfo", "")

            if debug or not found:
                log(f"🔍 查找可用场地 {time_text}: {debug_info}")

            return found, court_index, court_name, total_seats, debug_info
        return False, 0, "", 0, "返回结果格式错误"
    except Exception as e:
        log(f"⚠ 查找可用场地失败：{e}")
        return False, 0, "", 0, str(e)


def quick_check_time_slot(driver, time_text, debug=False):
    """快速检查时间段的可用场地数量，返回 (timeFound, availableCount, totalSeats, debugInfo)"""
    try:
        result = driver.execute_async_script(QUICK_CHECK_AVAILABILITY_JS, time_text, debug)
        if isinstance(result, dict):
            time_found = result.get("timeFound", False)
            available_count = result.get("availableCount", 0)
            total_seats = result.get("totalSeats", 0)
            bought_count = result.get("boughtCount", 0)
            debug_info = result.get("debugInfo", "")
            sample_classes = result.get("sampleClasses", [])

            # 输出详细调试信息
            if debug or available_count == 0:
                log(f"🔍 快速检查 {time_text}: {debug_info}")
                if sample_classes:
                    log(f"   样本class: {sample_classes[:3]}")

            return time_found, available_count, total_seats, bought_count
        return False, 0, 0, 0
    except Exception as e:
        log(f"⚠ 快速检查时间段失败：{e}")
        return False, 0, 0, 0


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
        info = res.get("info", "")
        log(f"时间{time_text} 场地{court_index} -> {status}")
        if info:
            log(f"   详情: {info}")

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


def click_submit_order_button(driver, timeout: float = 2.0) -> bool:
    """点击"立即下单/提交订单"等按钮。"""
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
        time.sleep(0.05)
    log("❌ 未能点击立即下单按钮。")
    return False


def confirm_if_needed(driver):
    """尝试点击确认/确定；若无弹窗则忽略"""
    try:
        confirm = WebDriverWait(driver, 0.5).until(
            EC.element_to_be_clickable((By.XPATH, "//button[contains(., '确认') or contains(., '确定')]"))
        )
        confirm.click()
        log("已点击确认/确定")
        return True
    except Exception:
        return False

def handle_booking_notice_dialog(driver, timeout=3):
    """处理预订须知对话框：勾选复选框并点击提交订单"""
    try:
        # 等待对话框出现
        dialog = WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.XPATH, "//div[@role='dialog']//span[contains(text(), '预订须知')]"))
        )
        log("✓ 检测到预订须知对话框")
        time.sleep(0.05)

        # 查找并勾选复选框
        checkbox_clicked = False
        try:
            # 方法1: 直接点击 checkbox input
            checkbox = driver.find_element(By.XPATH, "//label[@class='el-checkbox']//input[@type='checkbox']")
            if not checkbox.is_selected():
                checkbox.click()
                log("✓ 已勾选预订须知复选框")
                checkbox_clicked = True
        except Exception:
            pass

        if not checkbox_clicked:
            try:
                # 方法2: 点击 label 元素
                checkbox_label = driver.find_element(By.XPATH, "//label[@class='el-checkbox']")
                checkbox_label.click()
                log("✓ 已勾选预订须知复选框(label)")
                checkbox_clicked = True
            except Exception:
                pass

        if not checkbox_clicked:
            try:
                # 方法3: 点击包含文本的 label
                checkbox_label = driver.find_element(By.XPATH, "//label[contains(., '本人已认真阅读')]")
                checkbox_label.click()
                log("✓ 已勾选预订须知复选框(text)")
                checkbox_clicked = True
            except Exception:
                log("⚠ 未能勾选复选框")

        time.sleep(0.05)

        # 点击"提交订单"按钮
        submit_button_clicked = False
        try:
            submit_btn = driver.find_element(By.XPATH, "//button[contains(., '提交订单')]")
            submit_btn.click()
            log("✓ 已点击提交订单按钮")
            submit_button_clicked = True
        except Exception:
            try:
                # 尝试用 JavaScript 点击
                submit_btn = driver.find_element(By.XPATH, "//button[@class='el-button btnStyle el-button--primary']")
                driver.execute_script("arguments[0].click();", submit_btn)
                log("✓ 已点击提交订单按钮(JS)")
                submit_button_clicked = True
            except Exception:
                log("❌ 未能点击提交订单按钮")

        return checkbox_clicked and submit_button_clicked

    except TimeoutException:
        # 没有对话框，这是正常的
        return True
    except Exception as e:
        log(f"⚠ 处理预订须知对话框时出错：{e}")
        return False

def wait_for_payment_page(driver, timeout=5):
    """
    等待跳转到支付页面（表示预订成功）
    支付页面特征：URL变化、包含"支付"等关键字
    """
    bad_words = ["已满", "不可选", "失败", "无可用", "库存不足", "预约次数已用尽", "超过限额"]
    payment_keywords = ["支付", "payment", "pay", "订单详情", "待支付"]

    initial_url = driver.current_url
    end = time.time() + timeout

    while time.time() < end:
        try:
            current_url = driver.current_url
            txt = driver.find_element(By.TAG_NAME, "body").text

            # 检查失败信息
            if any(w in txt for w in bad_words):
                log(f"检测到失败信息：{[w for w in bad_words if w in txt]}")
                return False

            # 检查URL是否变化（跳转到其他页面）
            if current_url != initial_url:
                log(f"✓ 检测到页面跳转: {initial_url} → {current_url}")
                # 检查是否包含支付相关关键字
                if any(kw in txt for kw in payment_keywords):
                    log(f"✓ 确认跳转到支付页面")
                    return True
                # 即使没有关键字，URL变化也可能表示成功
                log(f"✓ URL已变化，可能已成功")
                time.sleep(0.5)  # 再等0.5秒确认
                return True

            # 检查当前页面是否出现支付关键字
            if any(kw in txt for kw in payment_keywords):
                log(f"✓ 页面出现支付相关内容")
                return True

        except Exception as e:
            log(f"⚠ 检测页面状态时出错: {e}")
            pass
        time.sleep(0.1)

    log("⏱ 超时：未检测到跳转到支付页面")
    return False

def booking_flow(driver, config: BookingConfig, start_time_index=0):
    """
    优化的预订流程：对每个时间段，自动查找第一个可用场地
    返回: (success, last_time_index)
    """
    target_date = datetime.now(TZ) + timedelta(days=config.date_offset_days)
    log(
        "准备预约日期：{} (今天+{})".format(
            target_date.strftime("%Y-%m-%d"), config.date_offset_days
        )
    )
    if not click_date_tab(driver, target_date):
        log("❌ 日期未选中，无法继续。")
        return False, 0

    # 从指定位置开始遍历时间行
    for time_idx, t in enumerate(config.preferred_slots):
        # 如果还没到开始位置，跳过
        if time_idx < start_time_index:
            continue

        log(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        log(f"🔍 扫描时间段: {t}")

        # 快速检查这个时间段是否有可用场地
        time_found, available_count, total_seats, bought_count = quick_check_time_slot(
            driver, t, debug=config.debug
        )

        if not time_found:
            log(f"⏭️ 时间段 {t} 未找到，跳过")
            continue

        # 快速跳过策略：如果所有场地都已订，直接跳过
        if available_count == 0 and bought_count == total_seats and total_seats > 0:
            log(f"⏭️ 时间段 {t} 全部已订 ({bought_count}/{total_seats})，快速跳过")
            continue

        if available_count == 0:
            log(f"⚠️ 时间段 {t} 检测显示0个可用，尝试智能查找")

        # 使用智能查找获取第一个可用场地（总是启用debug以便排查问题）
        found, court_index, court_name, total_seats, debug_info = find_first_available_court(
            driver, t, debug=True
        )

        if not found:
            log(f"⏭️ 时间段 {t} 无可用场地，跳过")
            continue

        log(f"✅ 发现可用场地: {court_name} (编号{court_index})")

        # 尝试预订该场地
        ok = False
        for retry in range(config.click_retries):
            log(f"尝试预约：时间 {t}，{court_name} (第{retry+1}/{config.click_retries}次)")

            if strict_select_slot(driver, t, court_index, config):
                ok = True
                break

            # 失败后极短延迟重试
            if retry < config.click_retries - 1:
                time.sleep(0.05)

        if not ok:
            log(f"⚠️ 场地 {court_name} 点击失败，尝试下一时间段")
            continue

        # 选中场地后立即下单
        log(f"✅ 成功选中场地：{t} {court_name}，立即下单")

        # 智能等待下单按钮可用
        if not smart_wait_for_any_condition(
            driver,
            [lambda: click_submit_order_button(driver, timeout=0.5)],
            timeout=2.0
        ):
            log("⚠️ 未能点击立即下单按钮，尝试下一时间段")
            continue

        # 处理预订须知对话框（勾选复选框并点击提交订单）
        time.sleep(0.1)  # 等待对话框出现
        if not handle_booking_notice_dialog(driver, timeout=2.0):
            log("⚠️ 未能处理预订须知对话框，尝试下一时间段")
            # 记录失败位置，以便重试时继续
            return False, time_idx

        confirm_if_needed(driver)

        # 检测是否跳转到支付页面（表示成功）
        if wait_for_payment_page(driver, timeout=5):
            log(f"🎉🎉🎉 预约成功！场地: {court_name}，时间: {t} 🎉🎉🎉")
            log(f"✓ 已跳转到支付页面，预订完成！")
            return True, time_idx
        else:
            log(f"⚠️ {court_name} {t}时间提交失败，可能被其他人抢走了")
            # 提交失败，返回失败位置以便立即重试
            return False, time_idx

    log("❌ 全部时间段尝试完毕，未成功。")
    return False, len(config.preferred_slots) - 1

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
        help="优先尝试的时间段，逗号分隔 (默认: %(default)s)。对每个时间段会自动查找第一个可用场地",
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

        # 失败重试机制的最大次数
        max_retry_attempts = 5
        retry_count = 0
        last_time_idx = 0

        if args.now:
            if not navigate_to_activity(driver, config, force_home=True):
                log("❌ 无法自动跳转至目标场馆/活动，结束任务。")
                return
            log("立即执行 (--now)")

            # 尝试预约，带重试机制
            while retry_count < max_retry_attempts:
                success, last_time_idx = booking_flow(
                    driver, config, last_time_idx
                )

                if success:
                    log("🎉🎉🎉 预约成功！任务完成！🎉🎉🎉")
                    break

                retry_count += 1
                if retry_count < max_retry_attempts:
                    log(f"⚠️ 提交失败，立即刷新页面重试 (第 {retry_count}/{max_retry_attempts} 次)")
                    log(f"📍 将从上次位置继续：时间索引 {last_time_idx}")

                    # 快速刷新页面
                    driver.refresh()
                    wait_document_ready(driver, timeout=5)

                    # 重新导航到场馆
                    if not navigate_to_activity(driver, config, force_home=False):
                        log("⚠️ 导航失败，尝试从主页重新进入")
                        if not navigate_to_activity(driver, config, force_home=True):
                            log("❌ 无法重新进入场馆，结束重试")
                            break
                else:
                    log(f"❌ 已达到最大重试次数 ({max_retry_attempts})，任务结束")

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

            # 尝试预约，带重试机制
            while retry_count < max_retry_attempts:
                success, last_time_idx = booking_flow(
                    driver, config, last_time_idx
                )

                if success:
                    log("🎉🎉🎉 预约成功！任务完成！🎉🎉🎉")
                    break

                retry_count += 1
                if retry_count < max_retry_attempts:
                    log(f"⚠️ 提交失败，立即刷新页面重试 (第 {retry_count}/{max_retry_attempts} 次)")
                    log(f"📍 将从上次位置继续：时间索引 {last_time_idx}")

                    # 快速刷新页面
                    driver.refresh()
                    wait_document_ready(driver, timeout=5)

                    # 重新导航到场馆
                    if not navigate_to_activity(driver, config, force_home=False):
                        log("⚠️ 导航失败，尝试从主页重新进入")
                        if not navigate_to_activity(driver, config, force_home=True):
                            log("❌ 无法重新进入场馆，结束重试")
                            break
                else:
                    log(f"❌ 已达到最大重试次数 ({max_retry_attempts})，任务结束")

        log("完成，8 秒后关闭浏览器。")
        time.sleep(8)
    finally:
        try:
            driver.quit()
        except Exception:
            pass

if __name__ == "__main__":
    main()