# 履约效率&质量看板 Skill

## 概述
每周自动从飞书邮件附件读取订单宽表数据，按确认口径计算履约指标，生成本地 HTML 看板。

---

## 环境依赖

```bash
pip install requests pandas openpyxl python-dotenv
```

---

## 配置文件

项目根目录创建 `.env`：

```
FEISHU_APP_ID=your_app_id
FEISHU_APP_SECRET=your_app_secret
EMAIL_SUBJECT_KEYWORD=每周数据
```

飞书应用需开通权限：
- `mail:mail.messages:readonly`
- `mail:mail.attachments:readonly`

---

## 数据源说明

**数据来源**：飞书邮件附件（Excel），字段为聚合上门宽表明细。

**周维度基准字段**：`sign_time`（工程师签到时间）

**关键原始字段映射**：

| 字段名 | 含义 |
|--------|------|
| `sign_time` | 签到时间 |
| `first_photo_shot_complete_time` | 首次提交照片时间 |
| `first_start_take_photo_time` | 开始拍照时间 |
| `merchant_first_offer_price_time` | 首次报价时间 |
| `merchant_last_offer_price_time` | 末次报价时间 |
| `finish_time` | 订单完结时间 |
| `suspended_flag` | 是否暂停（1=暂停） |
| `increment_type_id` | 是否增单（1=增单，0=非增） |
| `on_work_days` | 在职时长（天） |
| `refuse_num` | 驳回次数 |
| `recheck_num` | 复检次数 |
| `state` | 订单状态（80=成交） |
| `admin_quality_cate_name` | 上门品类 |
| `region_name` | 大区 |

---

## 衍生字段计算口径

```python
是否签到   = sign_time 不为空
是否拍照   = first_photo_shot_complete_time 不为空
是否报价   = merchant_first_offer_price_time 不为空
是否驳回   = refuse_num > 0
多次驳回   = refuse_num > 1
是否复检   = recheck_num > 0
多次复检   = recheck_num > 1
是否暂停   = suspended_flag == 1
是否增单   = increment_type_id == 1
是否老人   = on_work_days >= 180（天）
是否成交   = state == 80

首次拍照时长(min) = (first_photo_shot_complete_time - first_start_take_photo_time) / 60
拍照报价时长(min) = (merchant_last_offer_price_time - first_start_take_photo_time) / 60
拍照准备时长(min) = (first_start_take_photo_time - sign_time) / 60
签到完结时长(min) = (finish_time - sign_time) / 60
# 以上时长负值视为异常，清洗为 NaN
```

---

## 指标计算口径

### 分母口径总表

| 指标 | 分母 | 剔除暂停单 |
|------|------|-----------|
| 单均签到完结时长 | 签到单（sign_time 不为空） | ✅ 是 |
| 单均拍照报价时长 | 报价单（是否报价=1） | ✅ 是 |
| 单均首次拍照时长 | 拍照完成单（是否拍照=1） | ❌ 否 |
| 拍照及时完成率 | 拍照完成单（是否拍照=1） | ❌ 否 |
| 履约超时率 | 签到完结时长有效单（非暂停+签到） | ✅ 是 |
| 驳回率 | 拍照完成单（是否拍照=1） | ❌ 否 |
| 复检率 | 拍照完成单（是否拍照=1） | ❌ 否 |
| 报价成交率 | 报价单（是否报价=1） | ❌ 否 |
| 多次驳回占比 | 驳回单（是否驳回=1） | ❌ 否 |
| 多次复检占比 | 复检单（是否复检=1） | ❌ 否 |

### 指标公式

```
单均签到完结时长  = 签到完结时长求和 / 签到单量          （剔除暂停单）
单均拍照报价时长  = 拍照报价时长求和 / 报价单量          （剔除暂停单）
单均首次拍照时长  = 首次拍照时长求和 / 拍照完成单量
拍照及时完成率   = 首次拍照时长≤6min 订单数 / 拍照完成单量
履约超时率      = 签到完结时长≥30min 订单数 / 签到完结时长有效单量（剔除暂停单）
驳回率         = 驳回次数>0 订单数 / 拍照完成单量
复检率         = 复检次数>0 订单数 / 拍照完成单量
报价成交率      = state=80 订单数 / 报价单量
多次驳回占比    = 驳回次数>1 订单数 / 驳回单量
多次复检占比    = 复检次数>1 订单数 / 复检单量
```

---

## 看板结构

**第一行：大盘整体**
- 2行 × 5列，共10个指标卡片
- 每卡片：指标名 + 最新周数值 + 折线图
- hover 显示对应周数据

**第二行：品类 × 新人/老人**
- 表格形式，纵轴：品类整体 → 新人 → 老人
- 横轴：10个指标
- 品类整体行：正常字号，浅灰背景
- 新人/老人行：弱化字号，纯白背景，左侧色条区分（新人橙，老人绿）

---

## 执行方式

### 手动执行
```bash
cd /your/project/path
python agent.py
```

生成文件路径：`./dashboard_output/履约效率_质量看板_YYYYMMDD.html`

### 每周自动执行（Mac/Linux cron）
```bash
crontab -e
# 每周一上午 10:00 执行
0 10 * * 1 cd /your/project/path && python agent.py
```

### Windows 任务计划程序
- 触发器：每周一上午 10:00
- 操作：`python /your/project/path/agent.py`

---

## 注意事项

1. **字段口径以本文件为准**，如数据源字段名变更需同步更新 `agent.py` 中的 `col_rename` 映射
2. **新人/老人阈值**：在职时长 ≥ 180 天为老人，如调整修改 `agent.py` 顶部 `SENIOR_THRESHOLD_DAYS`
3. **邮件关键词**：`.env` 中 `EMAIL_SUBJECT_KEYWORD` 需匹配每周数据邮件标题
4. **数据异常处理**：时长负值自动清洗为 NaN，不参与均值计算
5. **单周数据**：折线图显示为单点，累积多周后自动变为折线
