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
EMAIL_SUBJECT_KEYWORD=聚合上门宽表明细（近7天）
```

飞书应用需开通权限：
- `mail:mail.messages:readonly`
- `mail:mail.attachments:readonly`
- `im:message`
- `im:message:send_as_bot`
- `im:resource`

如需在每周生成后向授权用户本人发送看板通知，还需在飞书开放平台开启“机器人”能力并发布上述新增权限。通知由应用机器人通过 tenant_access_token 发送到 `FEISHU_NOTIFICATION_RECEIVER_EMAIL`（未配置时默认 `chenqiriga@zhuanzhuan.com`）。邮件读取仍使用 user_access_token。

如同时新增或更新了邮件读取相关的 OAuth 权限，再在本机一次性重新 OAuth 授权：

```bash
cd /your/project/path
.venv/bin/python agent.py --authorize
```

该命令只打开授权流程，不读取邮件或改写历史数据。授权成功后，程序会自动恢复为 refresh token 自动续期模式，后续 cron 无需改动。

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

### 定时任务（每周一 10:00 自动执行）

```bash
cd /Users/ga/Downloads/files && /Users/ga/Downloads/files/.venv/bin/python agent.py >> logs/agent.log 2>&1
```

### 完整执行流程

**飞书邮件取数**

- OAuth `user_access_token` 自动刷新
- 仅匹配周一当天收到、且标题关键词为「聚合上门宽表明细（近7天）」的邮件
- 若未找到对应周一邮件：立即停止，不合并历史数据、不生成或推送看板，并通过飞书机器人提醒负责人确认
- 下载 Excel/CSV 附件

**合并历史数据**

- 清洗本周数据
- 与 `dashboard_output/history_data.json.gz` 合并（压缩存储）
- 保留最近 13 周滚动数据（约3个月），可通过 `KEEP_WEEKS` 参数调整
- 以 `sign_time` 计算周维度全部指标

**生成看板**

- 当日文件：`dashboard_output/履约效率_质量看板_YYYYMMDD.html`
- 同步更新：`index.html`

**飞书妙搭发布（主用）**

- 自动发布 `index.html` 到主看板应用：[https://zhuanspirit.feishuapp.com/app/app_17bhqpwvvhv](https://zhuanspirit.feishuapp.com/app/app_17bhqpwvvhv)
- 自动发布 `anomaly.html` 到异常巡检应用：[https://zhuanspirit.feishuapp.com/app/app_17bhr25tbjd](https://zhuanspirit.feishuapp.com/app/app_17bhr25tbjd)

**GitHub Pages 更新（备份）**

- 自动 commit 并 push `index.html`、`anomaly.html`
- 备份链接：[https://ga267.github.io/1/](https://ga267.github.io/1/)

**飞书机器人通知**

- 发送对象：`chenqiriga@zhuanzhuan.com`
- 先发文字消息，再发 `index.html` 附件

### 查看方式

- 主看板（妙搭）：[https://zhuanspirit.feishuapp.com/app/app_17bhqpwvvhv](https://zhuanspirit.feishuapp.com/app/app_17bhqpwvvhv)
- 异常巡检（妙搭）：[https://zhuanspirit.feishuapp.com/app/app_17bhr25tbjd](https://zhuanspirit.feishuapp.com/app/app_17bhr25tbjd)
- GitHub Pages 备份：[https://ga267.github.io/1/](https://ga267.github.io/1/)
- 飞书附件：每周一收到通知后下载 HTML，用浏览器打开
- 本地：直接打开 `/Users/ga/Downloads/files/index.html`

---

## 注意事项

1. **字段口径以本文件为准**，如数据源字段名变更需同步更新 `agent.py` 中的 `col_rename` 映射
2. **新人/老人阈值**：在职时长 ≥ 180 天为老人，如调整修改 `agent.py` 顶部 `SENIOR_THRESHOLD_DAYS`
3. **邮件关键词**：`.env` 中 `EMAIL_SUBJECT_KEYWORD` 需匹配“聚合上门宽表明细（近7天）”邮件标题
4. **数据异常处理**：时长负值自动清洗为 NaN，不参与均值计算
5. **单周数据**：折线图显示为单点，累积多周后自动变为折线
