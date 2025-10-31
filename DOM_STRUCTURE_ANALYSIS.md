# 场地预约页面DOM结构分析

## 实际HTML结构

```
<ul class="leftUl fl">          ← 左侧时间列表
  <li>07:00</li>                ← 索引0
  <li>08:00</li>                ← 索引1
  <li>09:00</li>                ← 索引2
  ...
  <li>21:00</li>                ← 索引14
  <li>22:00</li>                ← 索引15
</ul>

<div class="tables fl">         ← 右侧座位区域
  <div>                         ← 匿名容器1
    <div style="width: 660px">  ← 匿名容器2
      <div class="clearfix">    ← 最外层clearfix (容器，不是座位行!)
        <div class="inner-seat-wrapper clearfix">  ← 包装器 (容器，不是座位行!)

          <ul style="position: absolute">  ← 场地标题行
            <li>场地1</li>
            <li>场地2</li>
            ...
            <li>场地16</li>
          </ul>

          <div class="clearfix">    ← 第1行座位 = 07:00 (索引0)
            <div class="seat"><div class="inner-seat bought-seat">...</div></div>  ← 场地1
            <div class="seat"><div class="inner-seat bought-seat">...</div></div>  ← 场地2
            ...x16个seat
          </div>

          <div class="clearfix">    ← 第2行座位 = 08:00 (索引1)
            <div class="seat"><div class="inner-seat bought-seat">...</div></div>
            ...x16个seat
          </div>

          ...共16个座位行 (对应16个时间段)
        </div>
      </div>
    </div>
  </div>
</div>
```

## 关键点

1. **时间和行的对应**：
   - li[0] (07:00) → seatRows[0] (第1个clearfix with seats)
   - li[1] (08:00) → seatRows[1] (第2个clearfix with seats)
   - ...
   - li[15] (22:00) → seatRows[15] (第16个clearfix with seats)

2. **场地编号和列的对应**：
   - seat[0] = 场地1
   - seat[1] = 场地2
   - ...
   - seat[15] = 场地16

3. **class状态**：
   - `inner-seat bought-seat` = 已被预订
   - `inner-seat unselected-seat` = 可以预订

## 选择策略

```javascript
// 1. 找到div.tables
var tablesDiv = document.querySelector('div.tables');

// 2. 找到所有clearfix
var allClearfix = Array.from(tablesDiv.querySelectorAll('div.clearfix'));

// 3. 只保留直接包含div.seat子元素的clearfix（排除容器clearfix）
var seatRows = [];
for (var i = 0; i < allClearfix.length; i++) {
  var directSeats = Array.from(allClearfix[i].children).filter(function(child) {
    return child.classList.contains('seat');
  });
  if (directSeats.length > 0) {
    seatRows.push(allClearfix[i]);
  }
}

// 4. 现在 seatRows[timeIndex] 就是正确的那一行
// 5. 那一行中 seats[courtIndex - 1] 就是指定编号的场地
```
