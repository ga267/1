"""
履约效率&质量看板 Agent
执行时间：每周一 10:00 自动运行
数据范围：最近6周滚动窗口
数据源：飞书邮件附件（关键词匹配：每周数据）
输出路径：dashboard_output/履约效率_质量看板_YYYYMMDD.html
历史数据：dashboard_output/history_data.json
"""

import os, json, secrets, threading, random, shutil, subprocess, requests, pandas as pd, numpy as np
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from dotenv import load_dotenv, set_key
from urllib.parse import parse_qs, urlencode, urlparse

load_dotenv()

# ========== 配置 ==========
APP_ID     = os.getenv("FEISHU_APP_ID")
APP_SECRET = os.getenv("FEISHU_APP_SECRET")
EMAIL_KW   = os.getenv("EMAIL_SUBJECT_KEYWORD", "每周数据")
MAILBOX_ID = os.getenv("FEISHU_MAILBOX_ID", "chenqiriga@zhuanzhuan.com")
OAUTH_REDIRECT_URI = os.getenv("FEISHU_OAUTH_REDIRECT_URI", "http://127.0.0.1:8766/oauth/callback")
OUTPUT_DIR = Path(__file__).resolve().parent / "dashboard_output"
PROJECT_DIR = Path(__file__).resolve().parent
PUBLISH_PATH = PROJECT_DIR / "index.html"
# 持久化最近 6 周的已清洗数据；新附件合并后自动滚动淘汰更早周。
HISTORY_DATA_PATH = OUTPUT_DIR / "history_data.json"
LEGACY_HISTORY_DATA_PATH = OUTPUT_DIR / "履约历史数据.pkl"
HISTORY_COLUMNS = [
    "order_id", "sign_time", "admin_quality_cate_name", "群体", "week",
    "是否签到", "是否拍照", "是否报价", "是否驳回", "多次驳回",
    "是否复检", "多次复检", "是否暂停", "是否成交",
    "首次拍照时长", "拍照报价时长", "签到完结时长",
]
SENIOR_DAYS = 180   # 在职天数 >= 此值为老人
# ==========================

METRICS = [
    ("单均签到完结时长", "min"),
    ("单均拍照报价时长",  "min"),
    ("单均首次拍照时长",  "min"),
    ("履约超时率",       "%"),
    ("拍照及时完成率",   "%"),
    ("驳回率",          "%"),
    ("报价成交率",       "%"),
    ("复检率",          "%"),
    ("多次驳回占比",     "%"),
    ("多次复检占比",     "%"),
]

METRIC_FORMULAS = {
    "单均签到完结时长": "签到完结时长求和 / 签到单量（剔除暂停单）",
    "单均首次拍照时长": "首次拍照时长求和 / 拍照完成单量",
    "履约超时率": "签到完结时长≥30min 订单数 / 签到完结时长有效单量（剔除暂停单）",
    "拍照及时完成率": "首次拍照时长≤6min 订单数 / 拍照完成单量",
    "驳回率": "refuse_num>0 订单数 / 拍照完成单量",
    "报价成交率": "state=80 订单数 / 报价单量",
    "复检率": "recheck_num>0 订单数 / 拍照完成单量",
    "多次驳回占比": "refuse_num>1 订单数 / 驳回单量",
    "多次复检占比": "recheck_num>1 订单数 / 复检单量",
    "单均拍照报价时长": "拍照报价时长求和 / 报价单量（剔除暂停单）",
}

METRIC_DAILY_LABELS = {
    "单均签到完结时长": ("周日均签到单", "日均签到单"),
    "单均拍照报价时长": ("周日均报价单", "日均报价单"),
    "单均首次拍照时长": ("周日均拍照完成单", "日均拍照完成单"),
    "履约超时率": ("周日均超时单", "日均超时单"),
    "拍照及时完成率": ("周日均及时完成单", "日均及时完成单"),
    "驳回率": ("周日均驳回单", "日均驳回单"),
    "报价成交率": ("周日均成交单", "日均成交单"),
    "复检率": ("周日均复检单", "日均复检单"),
    "多次驳回占比": ("周日均多次驳回单", "日均多次驳回单"),
    "多次复检占比": ("周日均多次复检单", "日均多次复检单"),
}

# -------- 飞书 API --------
def get_token():
    r = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": APP_ID, "app_secret": APP_SECRET}
    ).json()
    if r.get("code") != 0:
        raise Exception(f"Token 获取失败: {r.get('msg')}")
    print("✅ Token 获取成功")
    return r["tenant_access_token"]

def refresh_user_token(refresh_token):
    """使用长期 refresh_token 无交互续期，供 cron 定时任务使用。"""
    response = requests.post(
        "https://open.feishu.cn/open-apis/authen/v2/oauth/token",
        headers={"Content-Type": "application/json; charset=utf-8"},
        json={
            "grant_type": "refresh_token",
            "client_id": APP_ID,
            "client_secret": APP_SECRET,
            "refresh_token": refresh_token,
        },
    ).json()
    if response.get("code") != 0:
        raise Exception(f"user_access_token 自动刷新失败: {response.get('msg')}")
    set_key(".env", "FEISHU_USER_ACCESS_TOKEN", response["access_token"])
    if response.get("refresh_token"):
        set_key(".env", "FEISHU_USER_REFRESH_TOKEN", response["refresh_token"])
    print("✅ user_access_token 已自动刷新")
    return response["access_token"]

def get_user_token():
    """通过用户 OAuth 授权获取仅本次运行使用的 user_access_token。"""
    existing_token = os.getenv("FEISHU_USER_ACCESS_TOKEN")
    refresh_token = os.getenv("FEISHU_USER_REFRESH_TOKEN")
    if existing_token and os.getenv("FEISHU_FORCE_OAUTH") != "1":
        if refresh_token:
            try:
                return refresh_user_token(refresh_token)
            except Exception as exc:
                print(f"⚠️ user_access_token 自动刷新失败，尝试使用现有 token：{exc}")
        print("✅ 使用 .env 中已有的 user_access_token")
        return existing_token

    parsed = urlparse(OAUTH_REDIRECT_URI)
    if parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise Exception("FEISHU_OAUTH_REDIRECT_URI 必须配置为本机回调地址")

    state = secrets.token_urlsafe(24)
    callback = {}
    done = threading.Event()

    class OAuthCallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            query = parse_qs(urlparse(self.path).query)
            callback["code"] = query.get("code", [None])[0]
            callback["state"] = query.get("state", [None])[0]
            callback["error"] = query.get("error", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<h2>飞书授权已接收，可关闭此页面并返回终端。</h2>".encode("utf-8"))
            done.set()

        def log_message(self, *_):
            pass

    server = HTTPServer((parsed.hostname, parsed.port or 80), OAuthCallbackHandler)
    threading.Thread(target=server.handle_request, daemon=True).start()
    auth_url = "https://accounts.feishu.cn/open-apis/authen/v1/authorize?" + urlencode({
        "client_id": APP_ID,
        "response_type": "code",
        "redirect_uri": OAUTH_REDIRECT_URI,
        "scope": "offline_access mail:user_mailbox.message:readonly mail:user_mailbox.message.subject:read mail:user_mailbox.message.body:read",
        "prompt": "consent",
        "state": state,
    })
    print(f"🔐 请在浏览器完成飞书授权：{auth_url}")
    if not done.wait(timeout=300):
        server.server_close()
        raise Exception("等待 OAuth 授权超时（5 分钟）")
    server.server_close()
    if callback.get("state") != state or callback.get("error") or not callback.get("code"):
        raise Exception(f"OAuth 授权失败: {callback.get('error') or '未收到有效授权码'}")

    response = requests.post(
        "https://open.feishu.cn/open-apis/authen/v2/oauth/token",
        headers={"Content-Type": "application/json; charset=utf-8"},
        json={
            "grant_type": "authorization_code",
            "client_id": APP_ID,
            "client_secret": APP_SECRET,
            "code": callback["code"],
            "redirect_uri": OAUTH_REDIRECT_URI,
        },
    ).json()
    if response.get("code") != 0:
        raise Exception(f"user_access_token 获取失败: {response.get('msg')}")
    set_key(".env", "FEISHU_USER_ACCESS_TOKEN", response["access_token"])
    if response.get("refresh_token"):
        set_key(".env", "FEISHU_USER_REFRESH_TOKEN", response["refresh_token"])
    print("✅ user_access_token 获取成功")
    return response["access_token"]

def get_mails(token):
    response = requests.get(
        f"https://open.feishu.cn/open-apis/mail/v1/user_mailboxes/{MAILBOX_ID}/messages",
        headers={"Authorization": f"Bearer {token}"},
        params={"page_size": 20, "folder_id": "INBOX"}
    )
    print(f"📨 邮件列表请求 URL：{response.url}")
    print(f"📨 邮件列表 Response Body：{response.text}")
    r = response.json()
    if r.get("code") != 0:
        raise Exception(f"邮件列表获取失败: {r.get('msg')}")

    mails = []
    for message_id in r.get("data", {}).get("items", []):
        detail = requests.get(
            f"https://open.feishu.cn/open-apis/mail/v1/user_mailboxes/{MAILBOX_ID}/messages/{message_id}",
            headers={"Authorization": f"Bearer {token}"},
        ).json()
        if detail.get("code") != 0:
            raise Exception(f"邮件详情获取失败: {detail.get('msg')}")
        mails.append(detail.get("data", {}).get("message", {}))
    return mails

def find_mail(mails):
    if not EMAIL_KW:
        return mails[0] if mails else None
    return next((m for m in mails if EMAIL_KW in m.get("subject", "")), None)

def download_attachment(token, message_id):
    detail = requests.get(
        f"https://open.feishu.cn/open-apis/mail/v1/user_mailboxes/{MAILBOX_ID}/messages/{message_id}",
        headers={"Authorization": f"Bearer {token}"},
    ).json()
    atts = detail.get("data", {}).get("message", {}).get("attachments", [])

    att = next((a for a in atts if a.get("filename","").endswith((".xlsx",".xls",".csv"))), None)
    if not att:
        raise Exception("未找到 Excel/CSV 附件")

    url_response = requests.get(
        f"https://open.feishu.cn/open-apis/mail/v1/user_mailboxes/{MAILBOX_ID}/messages/{message_id}/attachments/download_url",
        headers={"Authorization": f"Bearer {token}"},
        params={"attachment_ids": att["id"]},
    ).json()
    if url_response.get("code") != 0:
        raise Exception(f"附件下载链接获取失败: {url_response.get('msg')}")
    download_url = url_response.get("data", {}).get("download_urls", [{}])[0].get("download_url")
    if not download_url:
        raise Exception("附件下载链接为空")
    content = requests.get(download_url).content

    OUTPUT_DIR.mkdir(exist_ok=True)
    path = OUTPUT_DIR / att["filename"]
    path.write_bytes(content)
    print(f"✅ 附件下载完成：{path}")
    return path

# -------- 数据处理 --------
def load_and_clean(path):
    suffix = Path(path).suffix.lower()
    if suffix == ".csv":
        df = pd.read_csv(path)
    elif suffix in {".txt", ".tsv"}:
        df = pd.read_csv(path, sep="\t", low_memory=False)
    else:
        df = pd.read_excel(path)

    for col in ["sign_time","first_photo_shot_complete_time","merchant_first_offer_price_time",
                "merchant_last_offer_price_time","first_start_take_photo_time","finish_time"]:
        df[col] = pd.to_datetime(df[col], errors="coerce")

    df["是否签到"] = df["sign_time"].notna().astype(int)
    df["是否拍照"] = df["first_photo_shot_complete_time"].notna().astype(int)
    df["是否报价"] = df["merchant_first_offer_price_time"].notna().astype(int)
    df["是否驳回"] = (pd.to_numeric(df["refuse_num"], errors="coerce") > 0).astype(int)
    df["多次驳回"] = (pd.to_numeric(df["refuse_num"], errors="coerce") > 1).astype(int)
    df["是否复检"] = (pd.to_numeric(df["recheck_num"], errors="coerce") > 0).astype(int)
    df["多次复检"] = (pd.to_numeric(df["recheck_num"], errors="coerce") > 1).astype(int)
    df["是否暂停"] = (pd.to_numeric(df["suspended_flag"], errors="coerce") == 1).astype(int)
    df["是否增单"] = (pd.to_numeric(df["increment_type_id"], errors="coerce") == 1).astype(int)
    df["是否老人"] = (pd.to_numeric(df["on_work_days"], errors="coerce") >= SENIOR_DAYS).astype(int)
    df["是否成交"] = (pd.to_numeric(df["state"], errors="coerce") == 80).astype(int)
    df["群体"]    = df["是否老人"].map({1: "老人", 0: "新人"})

    df["首次拍照时长"] = (df["first_photo_shot_complete_time"] - df["first_start_take_photo_time"]).dt.total_seconds() / 60
    df["拍照报价时长"] = (df["merchant_last_offer_price_time"] - df["first_start_take_photo_time"]).dt.total_seconds() / 60
    df["签到完结时长"] = (df["finish_time"] - df["sign_time"]).dt.total_seconds() / 60

    for col in ["首次拍照时长", "拍照报价时长", "签到完结时长"]:
        df[col] = df[col].where(df[col] >= 0, np.nan)

    iso_week = df["sign_time"].dt.isocalendar()
    df["week"] = (
        iso_week["year"].astype("string").str[-2:]
        + "年-第"
        + iso_week["week"].astype("string").str.zfill(2)
        + "周"
    )
    print(f"✅ 数据加载完成：{len(df)} 条，{df['week'].nunique()} 周")
    return df

def merge_history(new_df):
    """将本周清洗后的数据追加到本地历史数据，不覆盖既有明细。"""
    OUTPUT_DIR.mkdir(exist_ok=True)
    if HISTORY_DATA_PATH.exists():
        history_df = pd.read_json(HISTORY_DATA_PATH, orient="records")
        history_df["sign_time"] = pd.to_datetime(history_df["sign_time"], errors="coerce")
        df = pd.concat([history_df, new_df], ignore_index=True)
        print(f"📚 已加载历史数据：{len(history_df)} 条；本次新增：{len(new_df)} 条")
    elif LEGACY_HISTORY_DATA_PATH.exists():
        history_df = pd.read_pickle(LEGACY_HISTORY_DATA_PATH)
        df = pd.concat([history_df, new_df], ignore_index=True)
        print(f"📚 已从旧历史文件迁移：{len(history_df)} 条；本次新增：{len(new_df)} 条")
    else:
        df = new_df.copy()
        print(f"📚 未找到历史数据，已以本次附件建立基线：{len(df)} 条")

    recent_weeks = sorted(df["week"].dropna().unique())[-6:]
    df = df[df["week"].isin(recent_weeks)].copy()
    history_columns = [column for column in HISTORY_COLUMNS if column in df.columns]
    history_df = df[history_columns].copy()
    tmp_path = HISTORY_DATA_PATH.with_suffix(".tmp")
    history_df.to_json(tmp_path, orient="records", force_ascii=False, date_format="iso")
    tmp_path.replace(HISTORY_DATA_PATH)
    print(f"📚 已保留最近 6 周：{'、'.join(recent_weeks)}")
    print(f"📚 历史数据已更新：{HISTORY_DATA_PATH.resolve()}")
    return df

def calc(g):
    g_ns        = g[g["是否暂停"] == 0]
    g_ns_sign   = g_ns[g_ns["是否签到"] == 1]
    g_ns_offer  = g_ns[g_ns["是否报价"] == 1]
    g_photo     = g[g["是否拍照"] == 1]
    g_offer     = g[g["是否报价"] == 1]
    g_reject    = g[g["是否驳回"] == 1]
    g_recheck   = g[g["是否复检"] == 1]
    g_deal      = g[g["是否成交"] == 1]
    g_pv        = g_photo[g_photo["首次拍照时长"].notna()]
    g_sv        = g_ns_sign[g_ns_sign["签到完结时长"].notna()]

    sm = lambda s: round(s.mean(), 2) if len(s.dropna()) > 0 else None
    sr = lambda n, d: round(len(n)/len(d)*100, 1) if len(d) > 0 else None

    return {
        "单均签到完结时长": sm(g_ns_sign["签到完结时长"]),
        "履约超时率":       sr(g_sv[g_sv["签到完结时长"] >= 30], g_sv),
        "单均首次拍照时长":  sm(g_photo["首次拍照时长"]),
        "拍照及时完成率":   sr(g_pv[g_pv["首次拍照时长"] <= 6], g_pv),
        "驳回率":          sr(g_reject, g_photo),
        "复检率":          sr(g_recheck, g_photo),
        "报价成交率":       sr(g_deal, g_offer),
        "多次驳回占比":     sr(g[g["多次驳回"]==1], g_reject),
        "多次复检占比":     sr(g[g["多次复检"]==1], g_recheck),
        "单均拍照报价时长":  sm(g_ns_offer["拍照报价时长"]),
        "日均成交单量":      round(len(g_deal) / 7, 1),
        "日均签到单":        round(len(g_ns_sign) / 7, 1),
        "日均报价单":        round(len(g_ns_offer) / 7, 1),
        "日均成交单":        round(len(g_deal) / 7, 1),
        "日均拍照完成单":    round(len(g_photo) / 7, 1),
        "日均超时单":        round(len(g_sv[g_sv["签到完结时长"] >= 30]) / 7, 1),
        "日均及时完成单":    round(len(g_pv[g_pv["首次拍照时长"] <= 6]) / 7, 1),
        "日均驳回单":        round(len(g_reject) / 7, 1),
        "日均复检单":        round(len(g_recheck) / 7, 1),
        "日均多次驳回单":    round(len(g[g["多次驳回"] == 1]) / 7, 1),
        "日均多次复检单":    round(len(g[g["多次复检"] == 1]) / 7, 1),
    }

def build_weekly(df, weeks):
    return {w: calc(df[df["week"] == w]) for w in weeks}

def build_week_display_labels(df, weeks):
    """由每周的 sign_time 推导周一至周日，用于看板的展示标签。"""
    labels = {}
    sign_times = pd.to_datetime(df["sign_time"], errors="coerce")
    for week_label in weeks:
        try:
            yy = int(week_label.split("年", 1)[0])
            week_no = int(week_label.split("第", 1)[1].split("周", 1)[0])
        except (ValueError, IndexError):
            labels[week_label] = week_label
            continue

        dates = sign_times[df["week"] == week_label].dropna()
        # 周日补数可能在次日跑出，优先使用与该 ISO 周一致的签到日期。
        if not dates.empty:
            iso = dates.dt.isocalendar()
            dates = dates[(iso["year"] == 2000 + yy) & (iso["week"] == week_no)]
        if not dates.empty:
            anchor = dates.iloc[0].normalize()
            start = anchor - pd.Timedelta(days=anchor.weekday())
        else:
            start = pd.Timestamp(datetime.fromisocalendar(2000 + yy, week_no, 1))
        end = start + pd.Timedelta(days=6)
        labels[week_label] = f"{week_label}（{start:%m%d}-{end:%m%d}）"
    return labels

def clean_json(obj):
    if isinstance(obj, dict):  return {k: clean_json(v) for k, v in obj.items()}
    if isinstance(obj, list):  return [clean_json(i) for i in obj]
    if isinstance(obj, float) and np.isnan(obj): return None
    if hasattr(obj, "item"):   return obj.item()
    return obj

# -------- 生成看板 --------
def generate_html(weeks, overall_weekly, cate_weekly, categories, week_display_labels=None, overall_groups=None):
    week_display_labels = week_display_labels or {w: w for w in weeks}
    overall_groups = overall_groups or {}
    def bdo(wd):
        s = {m: [wd.get(w, {}).get(m) for w in weeks] for m, u in METRICS}
        daily_deals = [wd.get(w, {}).get("日均成交单量") for w in weeks]
        daily_counts = {
            key: [wd.get(w, {}).get(key) for w in weeks]
            for key in {count_key for _, count_key in METRIC_DAILY_LABELS.values()}
        }
        return json.dumps({"weeks": weeks, "metrics": s, "dailyDeals": daily_deals, "dailyCounts": daily_counts}, ensure_ascii=False)

    overall_data = bdo(overall_weekly)
    overall_group_data = {group: bdo(overall_groups.get(group, {})) for group in ["新人", "老人"]}
    cate_data = {c: {g: bdo(cate_weekly[c].get(g, {})) for g in ["整体","新人","老人"]} for c in categories}
    metrics_list = [{"name": m, "unit": u} for m, u in METRICS]
    metric_formulas = json.dumps(METRIC_FORMULAS, ensure_ascii=False)
    title_emoji = random.choice(["⚡", "🤖", "🔮", "💎"])

    return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>履约效率&质量看板</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;background:#161b2e;color:#c8cce0;padding:32px 36px;min-width:1300px;text-align:left}}
h1{{font-size:24px;font-weight:700;color:#fff;margin-bottom:5px;letter-spacing:-.4px}}
.title-emoji{{margin-right:6px}}
.sub{{font-size:12px;color:#7b80a0;margin-bottom:28px}}
.stitle{{font-size:18px;font-weight:700;color:#fff;letter-spacing:-.15px;margin:30px 0 14px;padding-bottom:10px;border-bottom:1px solid #2e3a5c;text-align:left}}
.overall-grid{{display:grid;grid-template-columns:repeat(5,1fr);gap:12px}}
.card{{position:relative;overflow:hidden;background:#1e2540;border:1px solid #2e3a5c;border-radius:12px;padding:16px 16px 8px;cursor:crosshair;text-align:left;box-shadow:0 2px 8px rgba(0,0,0,.3)}}
.card::before{{content:'';position:absolute;z-index:1;left:0;top:0;bottom:0;width:3px;border-radius:0 2px 2px 0;background:#3a3d52}}
.card.accent-good::before{{background:#34d399}}
.card.accent-bad::before{{background:#f87171}}
.card.accent-neutral::before{{background:#3a3d52}}
.clabel{{font-size:12px;color:#a0aac4;margin-bottom:4px;white-space:nowrap;font-weight:500}}
.cval{{font-size:24px;font-weight:700;color:#fff;letter-spacing:-.5px}}
.delta{{height:16px;margin-top:3px;font-size:12px;color:#8b8fa8;line-height:16px}}
.delta.good{{color:#4ade80}}
.delta.bad{{color:#ff6b6b}}
.card-daily{{height:14px;margin-top:2px;font-size:11px;color:#6370a0;line-height:14px}}
.segment-list{{margin-top:8px;padding-top:7px;border-top:1px solid #2e3a5c}}
.segment-row{{display:grid;grid-template-columns:minmax(0,1fr) auto auto;gap:7px;align-items:baseline;font-size:11px;line-height:17px;white-space:nowrap}}
.segment-label{{color:#8090b8;font-size:10px;overflow:hidden;text-overflow:ellipsis}}
.segment-value{{font-size:11px;font-weight:600}}
.segment-value.newbie{{color:#fb7185}}
.segment-value.senior{{color:#34d399}}
.segment-delta{{font-size:10px;font-weight:500}}
.segment-delta.newbie{{color:#fb7185}}
.segment-delta.senior{{color:#34d399}}
.card svg{{width:100%;display:block}}
.card svg.main-spark{{height:46px}}
.card svg.daily-spark{{height:26px;margin-top:2px;opacity:.75}}
.cate-table{{width:100%;border-collapse:separate;border-spacing:0;background:#222a45;border:1px solid #28334f;border-radius:12px;overflow:hidden}}
.cate-table th{{background:#1a2035;color:#8890b0;font-size:12px;font-weight:600;padding:12px 10px;text-align:left;border-bottom:1px solid #28334f;white-space:nowrap;position:sticky;top:0;z-index:10}}
.cate-table th.rh{{position:sticky;text-align:left;min-width:150px;padding-left:16px}}
.cate-table th.rh::before{{content:'';position:absolute;left:0;top:0;bottom:0;width:3px;border-radius:0 2px 2px 0;background:#6366f1}}
.cate-head-note{{margin-top:2px;color:#6370a0;font-size:10px;font-weight:400;line-height:1.2}}
.cate-table td{{border-bottom:1px solid #28334f;padding:0;vertical-align:middle;text-align:left}}
.cate-table tr:last-child td{{border-bottom:0}}
.rl-total{{position:relative;font-size:14px;font-weight:700;color:#e0e4f4;background:#222a45;padding:12px 14px 12px 18px;white-space:nowrap;text-align:left}}
.rl-total::before{{content:'';position:absolute;left:0;top:0;bottom:0;width:3px;border-radius:0 2px 2px 0;background:#6366f1}}
.rl-newbie,.rl-senior{{font-size:12px;font-weight:400;color:#8090b8;background:#1a2238;padding:9px 14px 9px 30px;white-space:nowrap;text-align:left}}
.group-note{{margin-left:4px;color:#6370a0;font-size:10px;font-weight:400}}
.daily-deals{{margin-top:3px;color:#6370a0;font-weight:400;white-space:nowrap}}
.rl-total .daily-deals{{font-size:12px}}
.rl-newbie .daily-deals,.rl-senior .daily-deals{{font-size:10px}}
.cate-total-row{{cursor:pointer}}
.cate-total-row .rl-total::after{{content:'⌄';margin-left:6px;color:#c4c8e0;font-size:10px}}
.cate-total-row.open .rl-total::after{{content:'⌃'}}
.cate-sub-row{{display:none}}
.tc-total{{background:#222a45;padding:10px 12px 5px;cursor:crosshair;min-width:130px;text-align:left}}
.tc-total .tval{{font-size:15px;font-weight:600;color:#fff}}
.tc-total .delta{{font-size:12px}}
.tc-total svg{{height:34px;width:100%;display:block}}
.tc-sub{{background:#1a2238;padding:8px 12px 4px;cursor:crosshair;min-width:130px;text-align:left}}
.tc-sub .tval{{font-size:12px;font-weight:400;color:#8090b8}}
.tc-sub .delta{{font-size:11px;height:13px;line-height:13px}}
.tc-sub svg{{height:20px;width:100%;display:block}}
.tooltip{{position:fixed;background:#252839;border:1px solid #3a3d52;border-radius:8px;padding:7px 11px;font-size:11px;color:#fff;pointer-events:none;z-index:9999;display:none;white-space:nowrap;box-shadow:0 4px 16px rgba(0,0,0,.3)}}
.tooltip .tw{{color:#c4c8e0;font-size:10px;margin-bottom:2px}}
.tooltip .tv{{font-size:14px;font-weight:700;color:#fff}}
.qmark{{display:inline-flex;align-items:center;justify-content:center;width:13px;height:13px;margin-left:4px;border:1px solid #7b80a0;border-radius:50%;color:#c4c8e0;font-size:9px;line-height:1;cursor:pointer;vertical-align:1px}}
.formula{{position:fixed;z-index:10000;display:none;max-width:320px;padding:10px 12px;border:1px solid #3a3d52;border-radius:8px;background:#252839;color:#fff;font-size:12px;line-height:1.55;text-align:left;box-shadow:0 8px 28px rgba(0,0,0,.3)}}
.formula strong{{display:block;margin-bottom:3px;color:#fff}}
</style>
</head>
<body>
<h1><span class="title-emoji">{title_emoji}</span>履约效率&质量看板</h1>
<div class="sub">周签到维度 · 全量 · 最新数据：{week_display_labels.get(weeks[-1], weeks[-1]) if weeks else "—"}</div>
<div class="stitle">整体数据概览（未剔除）</div>
<div id="og" class="overall-grid"></div>
<div class="stitle">品类数据概览</div>
<div id="cs"></div>
<div class="tooltip" id="tip"><div class="tw" id="tw"></div><div class="tv" id="tv"></div></div>
<div class="formula" id="formula"></div>
<script>
const W={json.dumps(weeks,ensure_ascii=False)};
const WL={json.dumps(week_display_labels,ensure_ascii=False)};
const OA={overall_data};
const OG={json.dumps(overall_group_data,ensure_ascii=False)};
const CD={json.dumps(cate_data,ensure_ascii=False)};
const CA={json.dumps(categories,ensure_ascii=False)};
const MT={json.dumps(metrics_list,ensure_ascii=False)};
const MF={metric_formulas};
const MDL={json.dumps(METRIC_DAILY_LABELS, ensure_ascii=False)};
const CL={{ov:'#818cf8',dy:'#38bdf8',to:'#818cf8',nw:'#fb7185',sr:'#34d399'}};
const tip=document.getElementById('tip'),tw=document.getElementById('tw'),tv=document.getElementById('tv');
const formula=document.getElementById('formula');
let currentWeekIndex=Math.max(0,W.length-1);
const onWeekChange=[];
const chartSubscriptions=[];
const POSITIVE_METRICS=new Set(['拍照及时完成率','报价成交率']);
const RATE_METRICS=new Set(['履约超时率','拍照及时完成率','驳回率','报价成交率','复检率','多次驳回占比','多次复检占比']);
function subscribeWeekChange(callback){{onWeekChange.push(callback);callback(currentWeekIndex);}}
function subscribeChart(svg,info){{
  if(!info)return;
  chartSubscriptions.push({{svg,info}});
  markPoint(svg,info,currentWeekIndex,false);
}}
function syncChartHighlights(index,visible){{
  chartSubscriptions.forEach(item=>markPoint(item.svg,item.info,index,visible));
}}
function setCurrentWeek(index,showHighlights=true){{
  currentWeekIndex=Math.max(0,Math.min(W.length-1,index));
  onWeekChange.forEach(callback=>callback(currentWeekIndex));
  syncChartHighlights(currentWeekIndex,showHighlights);
}}
function formatDelta(vals,index,metricName){{
  const current=vals[index],previous=index>0?vals[index-1]:null;
  const isRate=RATE_METRICS.has(metricName);
  if(current==null||previous==null||(!isRate&&previous===0))return {{text:'-',cls:''}};
  const change=isRate?current-previous:(current-previous)/Math.abs(previous)*100;
  const suffix=isRate?'pp':'%';
  if(Math.abs(change)<0.05)return {{text:`0.0${{suffix}}`,cls:''}};
  const up=change>0,good=POSITIVE_METRICS.has(metricName)?up:!up;
  return {{text:`${{up?'▲':'▼'}}${{Math.abs(change).toFixed(1)}}${{suffix}}`,cls:good?'good':'bad'}};
}}
function updateMetricDisplay(valueEl,deltaEl,vals,index,metric){{
  const value=vals[index];
  valueEl.textContent=value!=null?value+metric.unit:'—';
  const delta=formatDelta(vals,index,metric.name);
  deltaEl.textContent=delta.text;
  deltaEl.className='delta '+delta.cls;
  const card=valueEl.closest('.card');
  if(card){{
    card.classList.remove('accent-good','accent-bad','accent-neutral');
    card.classList.add(delta.cls==='good'?'accent-good':delta.cls==='bad'?'accent-bad':'accent-neutral');
  }}
}}
function updateDailyDisplay(element,vals,index,label){{
  const value=vals[index];
  element.textContent=value!=null?`${{label}} ${{value}} 单`:`${{label}} — 单`;
}}
function updateSegmentDisplay(valueEl,deltaEl,vals,index,metric){{
  const value=vals[index];
  valueEl.textContent=value!=null?value+metric.unit:'—';
  deltaEl.textContent=formatDelta(vals,index,metric.name).text;
}}
function showFormula(name,e){{
  formula.innerHTML=`<strong>${{name}}</strong>${{MF[name]||'—'}}`;
  formula.style.display='block';
  formula.style.left=Math.min(window.innerWidth-340,e.clientX+10)+'px';
  formula.style.top=Math.min(window.innerHeight-90,e.clientY+12)+'px';
}}
document.addEventListener('click',e=>{{if(!e.target.classList.contains('qmark'))formula.style.display='none';}});
function sl(svg,vals,color,h,opts={{}}){{
  const pw=svg.parentElement?.clientWidth||140;
  svg.setAttribute('viewBox',`0 0 ${{pw}} ${{h}}`);
  const pts=vals.map((v,i)=>[i,v]).filter(([,v])=>v!=null);
  if(!pts.length)return null;
  const ys=pts.map(p=>p[1]),mn=Math.min(...ys),mx=Math.max(...ys),rng=mx!==mn?mx-mn:1,n=vals.length;
  const tx=xi=>n>1?(xi/(n-1))*(pw-8)+4:pw/2;
  const ty=yi=>h-((yi-mn)/rng)*(h*0.72)-h*0.1;
  const cp=pts.map(([xi,yi])=>[tx(xi),ty(yi)]);
  const pd='M'+cp.map(p=>`${{p[0].toFixed(1)}},${{p[1].toFixed(1)}}`).join(' L');
  const fd=pd+` L${{cp.at(-1)[0].toFixed(1)}},${{h}} L${{cp[0][0].toFixed(1)}},${{h}} Z`;
  const id=Math.random().toString(36).slice(2),lp=cp.at(-1);
  svg.innerHTML=`<defs><linearGradient id="g${{id}}" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0%" stop-color="${{color}}" stop-opacity="${{opts.fillOpacity??0.15}}"/>
    <stop offset="100%" stop-color="${{color}}" stop-opacity="0"/>
  </linearGradient></defs>
  <path d="${{fd}}" fill="url(#g${{id}})"/>
  <path d="${{pd}}" stroke="${{color}}" stroke-opacity="${{opts.opacity??1}}" stroke-width="${{opts.strokeWidth??1.25}}" stroke-dasharray="${{opts.dash||''}}" fill="none" stroke-linejoin="round" stroke-linecap="round"/>
  <circle cx="${{lp[0].toFixed(1)}}" cy="${{lp[1].toFixed(1)}}" r="${{opts.pointRadius??3.5}}" fill="${{color}}" fill-opacity="${{opts.opacity??1}}" stroke="#fff" stroke-width="1.5"/>
  <line id="hl${{id}}" x1="0" y1="0" x2="0" y2="${{h}}" stroke="${{color}}" stroke-width="1" stroke-dasharray="2,2" opacity="0" pointer-events="none"/>
  <circle id="hc${{id}}" cx="0" cy="0" r="3.5" fill="${{color}}" stroke="#fff" stroke-width="1.5" opacity="0" pointer-events="none"/>`;
  return {{cp,id,tx,ty,vals}};
}}
function markPoint(svg,info,index,visible=true){{
  if(!info)return;
  const v=info.vals[index],px=info.tx(index),py=v!=null?info.ty(v):0;
  const hl=svg.querySelector('#hl'+info.id),hc=svg.querySelector('#hc'+info.id);
  if(hl){{hl.setAttribute('x1',px);hl.setAttribute('x2',px);hl.setAttribute('opacity',visible&&v!=null?'0.4':'0');}}
  if(hc){{hc.setAttribute('cx',px);hc.setAttribute('cy',py);hc.setAttribute('opacity',visible&&v!=null?'1':'0');}}
}}
function hov(el,svg,info,unit,options={{}}){{
  if(!info)return;
  el.addEventListener('mousemove',e=>{{
    const r=svg.getBoundingClientRect();
    const ci=Math.max(0,Math.min(W.length-1,Math.round(((e.clientX-r.left)/r.width)*(W.length-1))));
    const v=info.vals[ci];
    setCurrentWeek(ci,true);
    tw.textContent=(WL[W[ci]]||W[ci]||'')+'：';tv.textContent=v!=null?`${{options.tooltipLabel?options.tooltipLabel+' ':''}}${{v}}${{unit}}`:'—';
    tip.style.display='block';tip.style.left=(e.clientX+14)+'px';tip.style.top=(e.clientY-10)+'px';
  }});
  el.addEventListener('mouseleave',()=>{{
    tip.style.display='none';
    setCurrentWeek(W.length-1,false);
  }});
}}
function mkCard(m,vals){{
  const d=document.createElement('div');d.className='card';
  const last=[...vals].reverse().find(v=>v!=null);
  d.innerHTML=`<div class="clabel">${{m.name}}<span class="qmark" title="查看计算口径">?</span></div><div class="cval">${{last!=null?last+m.unit:'—'}}</div><div class="delta">-</div><div class="card-daily">-</div><div class="segment-list"><div class="segment-row"><span class="segment-label">新人（在职&lt;180天）</span><span class="segment-value newbie">—</span><span class="segment-delta newbie">-</span></div><div class="segment-row"><span class="segment-label">老人（在职≥180天）</span><span class="segment-value senior">—</span><span class="segment-delta senior">-</span></div></div>`;
  d.querySelector('.qmark').addEventListener('click',e=>{{e.stopPropagation();showFormula(m.name,e);}});
  const valueEl=d.querySelector('.cval');
  const deltaEl=d.querySelector('.delta');
  const dailyEl=d.querySelector('.card-daily');
  const dailyConfig=MDL[m.name]||null;
  subscribeWeekChange(index=>updateMetricDisplay(valueEl,deltaEl,vals,index,m));
  if(dailyConfig)subscribeWeekChange(index=>updateDailyDisplay(dailyEl,OA.dailyCounts[dailyConfig[1]]||[],index,dailyConfig[0]));
  [['新人','newbie'],['老人','senior']].forEach(([group,cls])=>{{
    const groupData=OG[group]?JSON.parse(OG[group]):{{metrics:{{}}}};
    const groupVals=((groupData.metrics||{{}})[m.name]||Array(W.length).fill(null));
    const segmentValue=d.querySelector('.segment-value.'+cls);
    const segmentDelta=d.querySelector('.segment-delta.'+cls);
    subscribeWeekChange(index=>updateSegmentDisplay(segmentValue,segmentDelta,groupVals,index,m));
  }});
  const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.classList.add('main-spark');d.appendChild(svg);
  const dailySvg=document.createElementNS('http://www.w3.org/2000/svg','svg');dailySvg.classList.add('daily-spark');d.appendChild(dailySvg);
  requestAnimationFrame(()=>{{
    const info=sl(svg,vals,CL.ov,46,{{strokeWidth:2,pointRadius:3.5,fillOpacity:.15}});
    const dailyVals=dailyConfig?(OA.dailyCounts[dailyConfig[1]]||[]):[];
    const dailyInfo=sl(dailySvg,dailyVals,CL.dy,26,{{opacity:.6,strokeWidth:1.2,pointRadius:3.5,fillOpacity:0}});
    subscribeChart(svg,info);subscribeChart(dailySvg,dailyInfo);
    hov(svg,svg,info,m.unit);
    hov(dailySvg,dailySvg,dailyInfo,' 单',{{tooltipLabel:dailyConfig?dailyConfig[0]:'周日均'}});
  }});
  return d;
}}
function mkTC(m,vals,color,isSub){{
  const d=document.createElement('div');d.className=isSub?'tc-sub':'tc-total';
  const last=[...vals].reverse().find(v=>v!=null);
  const vd=document.createElement('div');vd.className='tval';vd.textContent=last!=null?last+m.unit:'—';
  d.appendChild(vd);
  const delta=document.createElement('div');delta.className='delta';d.appendChild(delta);
  subscribeWeekChange(index=>updateMetricDisplay(vd,delta,vals,index,m));
  const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');d.appendChild(svg);
  requestAnimationFrame(()=>{{const info=sl(svg,vals,color,isSub?20:34,{{strokeWidth:isSub?1.5:1.8,pointRadius:3.5}});subscribeChart(svg,info);hov(d,svg,info,m.unit);}});
  return d;
}}
const og=document.getElementById('og');
MT.forEach(m=>og.appendChild(mkCard(m,OA.metrics[m.name]||[])));
const cs=document.getElementById('cs');
const tbl=document.createElement('table');tbl.className='cate-table';
const th=document.createElement('thead'),hr=document.createElement('tr');
const t0=document.createElement('th');t0.className='rh';t0.innerHTML='<div>品类 / 维度</div><div class="cate-head-note">新人/老人按在职天数区分</div>';hr.appendChild(t0);
MT.forEach(m=>{{const th=document.createElement('th');th.textContent=m.name;hr.appendChild(th);}});
th.appendChild(hr);tbl.appendChild(th);
const tb=document.createElement('tbody');
CA.forEach(c=>{{
  const cd=CD[c]||{{}};
  [{{k:'整体',l:c,cls:'rl-total',color:CL.to,sub:false,note:''}},
   {{k:'新人',l:'新人',cls:'rl-newbie',color:CL.nw,sub:true,note:'（在职<180天）'}},
   {{k:'老人',l:'老人',cls:'rl-senior',color:CL.sr,sub:true,note:'（在职≥180天）'}}].forEach(row=>{{
    const tr=document.createElement('tr');
    tr.className=row.sub?'cate-sub-row':'cate-total-row';
    const rd=cd[row.k]?JSON.parse(cd[row.k]):{{metrics:{{}},dailyDeals:[]}};
    const td0=document.createElement('td');
    const rl=document.createElement('div');rl.className=row.cls;rl.innerHTML=row.l+(row.note?`<span class="group-note">${{row.note}}</span>`:'');
    const dailyDeals=document.createElement('div');dailyDeals.className='daily-deals';rl.appendChild(dailyDeals);
    subscribeWeekChange(index=>{{const v=(rd.dailyDeals||[])[index];dailyDeals.textContent=v!=null?`周日均 ${{v}} 单`:'周日均 — 单';}});
    td0.appendChild(rl);tr.appendChild(td0);
    MT.forEach(m=>{{
      const td=document.createElement('td');
      td.appendChild(mkTC(m,rd.metrics[m.name]||Array(W.length).fill(null),row.color,row.sub));
      tr.appendChild(td);
    }});
    tb.appendChild(tr);
    if(!row.sub){{
      tr.addEventListener('click',()=>{{
        tr.classList.toggle('open');
        let sibling=tr.nextElementSibling;
        while(sibling&&sibling.classList.contains('cate-sub-row')){{
          sibling.style.display=tr.classList.contains('open')?'table-row':'none';
          sibling=sibling.nextElementSibling;
        }}
      }});
    }}
  }});
}});
tbl.appendChild(tb);cs.appendChild(tbl);
</script>
</body>
</html>'''

def push_to_github():
    """提交并推送首页看板；本地未配置 Git 时不影响看板生成。"""
    if not (PROJECT_DIR / ".git").exists():
        print("⚠️ 未初始化 Git 仓库，跳过 GitHub Pages 推送")
        return
    try:
        subprocess.run(["git", "add", "index.html"], cwd=PROJECT_DIR, check=True)
        changed = subprocess.run(
            ["git", "diff", "--cached", "--quiet"], cwd=PROJECT_DIR
        ).returncode != 0
        if not changed:
            print("✅ 看板无变更，跳过 GitHub 推送")
            return
        subprocess.run(
            ["git", "commit", "-m", f"update dashboard {datetime.now().strftime('%Y%m%d')}"],
            cwd=PROJECT_DIR,
            check=True,
        )
        subprocess.run(["git", "push"], cwd=PROJECT_DIR, check=True)
        print("✅ GitHub Pages 看板已推送")
    except subprocess.CalledProcessError as exc:
        print(f"⚠️ GitHub 推送失败，不影响本地看板：{exc}")

# -------- 主流程 --------
def main():
    print("🚀 履约效率&质量看板 Agent 启动...")

    token = get_user_token()
    mails = get_mails(token)
    if not mails:
        raise Exception("邮件列表为空")

    mail = find_mail(mails)
    if not mail:
        raise Exception(f"未找到含「{EMAIL_KW}」的邮件")
    print(f"📬 目标邮件：{mail.get('subject')}")

    file_path = download_attachment(token, mail["message_id"])
    df = merge_history(load_and_clean(file_path))

    weeks = sorted(df["week"].dropna().unique())
    latest_week = weeks[-1]
    categories = list(
        df[df["week"] == latest_week].dropna(subset=["admin_quality_cate_name"])
          .groupby("admin_quality_cate_name")["是否成交"]
          .sum()
          .sort_values(ascending=False)
          .index
    )

    print("🧮 计算大盘指标...")
    overall_weekly = build_weekly(df, weeks)
    overall_groups = {
        "新人": build_weekly(df[df["群体"] == "新人"], weeks),
        "老人": build_weekly(df[df["群体"] == "老人"], weeks),
    }

    print("🧮 计算品类指标...")
    cate_weekly = {}
    for cate in categories:
        cdf = df[df["admin_quality_cate_name"] == cate]
        cate_weekly[cate] = {
            "整体": build_weekly(cdf, weeks),
            "新人": build_weekly(cdf[cdf["群体"]=="新人"], weeks),
            "老人": build_weekly(cdf[cdf["群体"]=="老人"], weeks),
        }

    overall_weekly = clean_json(overall_weekly)
    overall_groups = clean_json(overall_groups)
    cate_weekly    = clean_json(cate_weekly)

    print("🎨 生成看板...")
    html = generate_html(weeks, overall_weekly, cate_weekly, list(categories), build_week_display_labels(df, weeks), overall_groups)

    OUTPUT_DIR.mkdir(exist_ok=True)
    out = OUTPUT_DIR / f"履约效率_质量看板_{datetime.now().strftime('%Y%m%d')}.html"
    out.write_text(html, encoding="utf-8")
    shutil.copyfile(out, PUBLISH_PATH)
    print(f"🌐 Pages 首页已更新：{PUBLISH_PATH}")
    push_to_github()

    print(f"\n✅ 完成！看板路径：{out.resolve()}")

if __name__ == "__main__":
    main()
