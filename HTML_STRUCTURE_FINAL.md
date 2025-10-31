# 最终确认的HTML结构分析

基于用户提供的完整HTML，最终确认的结构：

## 1. 整体布局

```
┌─────────────────────────────────────────────┐
│ 运动类型tabs: 乒乓球 | 排球 | 篮球          │
├─────────────────────────────────────────────┤
│ 日期tabs: 10月31日 | 11月01日 | ...         │
├──────────┬──────────────────────────────────┤
│ 时间列表  │  场地网格                        │
│ ul.leftUl│  div.tables                      │
│          │                                  │
│ 07:00    │  [场地1][场地2]...[场地16]       │
│ 08:00    │  [  ✓  ][  ✓  ]...[  ✓  ]  ← 行1│
│ 09:00    │  [  ✓  ][  ✓  ]...[  ✓  ]  ← 行2│
│ ...      │  ...                             │
│ 22:00    │  [  ✓  ][  ✓  ]...[  ✓  ]  ← 行16│
└──────────┴──────────────────────────────────┘
```

## 2. 左侧时间列表

```html
<ul class="leftUl fl">
  <li>07:00</li>  <!-- 索引 0 -->
  <li>08:00</li>  <!-- 索引 1 -->
  <li>09:00</li>  <!-- 索引 2 -->
  <li>10:00</li>  <!-- 索引 3 -->
  <li>11:00</li>  <!-- 索引 4 -->
  <li>12:00</li>  <!-- 索引 5 -->
  <li>13:00</li>  <!-- 索引 6 -->
  <li>14:00</li>  <!-- 索引 7 -->
  <li>15:00</li>  <!-- 索引 8 -->
  <li>16:00</li>  <!-- 索引 9 -->
  <li>17:00</li>  <!-- 索引 10 -->
  <li>18:00</li>  <!-- 索引 11 -->
  <li>19:00</li>  <!-- 索引 12 -->
  <li>20:00</li>  <!-- 索引 13 -->
  <li>21:00</li>  <!-- 索引 14 -->
  <li>22:00</li>  <!-- 索引 15 -->
</ul>
```

共16个li

## 3. 右侧场地网格（关键结构）

```html
<div class="tables fl">
  <div>
    <div style="width: 660px; position: relative;">
      <div class="clearfix">  ← 外层容器clearfix（不是座位行！）

        <div class="inner-seat-wrapper clearfix">  ← 🔑 关键包裹容器

          <!-- 顶部场地标签（固定位置） -->
          <ul style="position: absolute; top: -8px;">
            <li title="场地1" class="topsiteStyle">场地1</li>
            <li title="场地2" class="topsiteStyle">场地2</li>
            ...
            <li title="场地16" class="topsiteStyle">场地16</li>
          </ul>

          <!-- 第1行：07:00的16个场地 -->
          <div class="clearfix">  ← 座位行1（对应li[0]=07:00）
            <div class="seat"><div class="inner-seat bought-seat">...</div></div>  ← 场地1
            <div class="seat"><div class="inner-seat bought-seat">...</div></div>  ← 场地2
            ...
            <div class="seat"><div class="inner-seat bought-seat">...</div></div>  ← 场地16
          </div>

          <!-- 第2行：08:00的16个场地 -->
          <div class="clearfix">  ← 座位行2（对应li[1]=08:00）
            <div class="seat">...</div>  ← 场地1
            <div class="seat">...</div>  ← 场地2
            ...
            <div class="seat">...</div>  ← 场地16
          </div>

          ...继续14行...

          <!-- 第16行：22:00的16个场地 -->
          <div class="clearfix">  ← 座位行16（对应li[15]=22:00）
            ...
          </div>

        </div>  ← inner-seat-wrapper结束

      </div>
    </div>
  </div>
</div>
```

## 4. 座位状态

每个座位的HTML：
```html
<div class="seat" style="width: 70px; height: 50px;">
  <div class="inner-seat bought-seat">  ← bought-seat = 已预订
    <div><img src="..." alt=""></div>
  </div>
</div>

或

<div class="seat" style="width: 70px; height: 50px;">
  <div class="inner-seat unselected-seat">  ← unselected-seat = 可预订
    <div>...</div>
  </div>
</div>
```

## 5. 正确的选择器策略

### ❌ 错误方法（之前使用的）：
```javascript
var tablesDiv = document.querySelector('div.tables');
var allClearfix = Array.from(tablesDiv.querySelectorAll('div.clearfix'));
// 这会选中所有clearfix，包括外层容器，导致数量不匹配
```

### ✅ 正确方法：
```javascript
// 方法1：直接选择inner-seat-wrapper，获取其子clearfix
var wrapper = document.querySelector('div.inner-seat-wrapper.clearfix');
if (wrapper) {
  var seatRows = Array.from(wrapper.children).filter(function(child) {
    return child.classList.contains('clearfix');
  });
  // 现在 seatRows.length = 16，与左侧时间列表完全对应
}

// 方法2：使用更精确的选择器
var seatRows = Array.from(document.querySelectorAll('div.inner-seat-wrapper.clearfix > div.clearfix'));
// 直接选择inner-seat-wrapper的直接子元素clearfix
```

## 6. 索引对应关系

```
左侧时间列表          右侧座位行
ul.leftUl > li     inner-seat-wrapper > div.clearfix

li[0]  = 07:00  →  seatRows[0]  (16个seat)
li[1]  = 08:00  →  seatRows[1]  (16个seat)
li[2]  = 09:00  →  seatRows[2]  (16个seat)
...
li[11] = 18:00  →  seatRows[11] (16个seat)
li[12] = 19:00  →  seatRows[12] (16个seat)
...
li[15] = 22:00  →  seatRows[15] (16个seat)
```

**每一行的场地对应**：
```
seatRows[X] > div.seat (按从左到右顺序)

seat[0]  = 场地1
seat[1]  = 场地2
seat[2]  = 场地3
...
seat[15] = 场地16
```

## 7. 完整的选择流程

要选择 "18:00的第3号场地"：

```javascript
// 1. 找到时间索引
var timeItems = Array.from(document.querySelectorAll('ul.leftUl > li'));
var timeIndex = -1;
for (var i = 0; i < timeItems.length; i++) {
  if (timeItems[i].textContent.trim() === '18:00') {
    timeIndex = i;  // 结果：11
    break;
  }
}

// 2. 获取座位行
var wrapper = document.querySelector('div.inner-seat-wrapper.clearfix');
var seatRows = Array.from(wrapper.children).filter(function(child) {
  return child.classList.contains('clearfix');
});

// 3. 选择对应行
var targetRow = seatRows[timeIndex];  // seatRows[11] = 18:00那一行

// 4. 获取该行的所有座位
var seats = Array.from(targetRow.querySelectorAll('div.seat'));

// 5. 选择第3号场地
var targetSeat = seats[2];  // 场地编号3 = 索引2

// 6. 检查状态并点击
var innerSeat = targetSeat.querySelector('.inner-seat');
var isAvailable = innerSeat.className.indexOf('unselected-seat') !== -1;
if (isAvailable) {
  innerSeat.click();
}
```

## 8. 关键点总结

1. ✅ **必须使用 `div.inner-seat-wrapper.clearfix` 作为容器**
2. ✅ **座位行 = inner-seat-wrapper的直接子元素中的clearfix**
3. ✅ **左侧16个li 完全对应 右侧16个座位行（div.clearfix）**
4. ✅ **每行16个seat 对应 场地1-16**
5. ✅ **不需要点击左侧时间li，直接用索引映射即可**
