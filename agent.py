"""
履约效率&质量看板 Agent
执行时间：每周一 10:00 自动运行
数据范围：最近6周滚动窗口
数据源：飞书邮件附件（关键词匹配：聚合上门宽表明细（近7天））
输出路径：dashboard_output/履约效率_质量看板_YYYYMMDD.html
历史数据：dashboard_output/history_data.json
"""

import os, sys, json, secrets, threading, random, shutil, subprocess, requests, pandas as pd, numpy as np, html as html_lib
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from dotenv import load_dotenv, set_key
from urllib.parse import parse_qs, urlencode, urlparse

load_dotenv()

# ========== 配置 ==========
APP_ID     = os.getenv("FEISHU_APP_ID")
APP_SECRET = os.getenv("FEISHU_APP_SECRET")
EMAIL_KW   = os.getenv("EMAIL_SUBJECT_KEYWORD", "聚合上门宽表明细（近7天）")
MAILBOX_ID = os.getenv("FEISHU_MAILBOX_ID", "chenqiriga@zhuanzhuan.com")
NOTIFICATION_RECEIVER_EMAIL = os.getenv("FEISHU_NOTIFICATION_RECEIVER_EMAIL", "chenqiriga@zhuanzhuan.com")
OAUTH_REDIRECT_URI = os.getenv("FEISHU_OAUTH_REDIRECT_URI", "http://127.0.0.1:8766/oauth/callback")
OUTPUT_DIR = Path(__file__).resolve().parent / "dashboard_output"
PROJECT_DIR = Path(__file__).resolve().parent
PUBLISH_PATH = PROJECT_DIR / "index.html"
# 持久化最近 6 周的已清洗数据；新附件合并后自动滚动淘汰更早周。
HISTORY_DATA_PATH = OUTPUT_DIR / "history_data.json"
LEGACY_HISTORY_DATA_PATH = OUTPUT_DIR / "履约历史数据.pkl"
HISTORY_COLUMNS = [
    "order_id", "sign_time", "create_time", "cancel_time", "finish_time", "increment_type_id",
    "签到时间", "完结时间", "admin_quality_cate_name", "last_admin_name", "user_id",
    "region_name", "fight_area_name", "群体", "week",
    "是否签到", "是否拍照", "是否报价", "是否驳回", "多次驳回",
    "是否复检", "多次复检", "是否质检", "是否暂停", "是否成交",
    "驳回次数", "复检次数",
    "首次拍照时长", "拍照报价时长", "签到完结时长", "议价时长", "报价次数",
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
    "签到单量": "签到时间不为空且非暂停订单数",
    "有效签到单量": "签到时间、完结时间均不为空且非暂停的订单数",
    "拍照完成单量": "first_photo_shot_complete_time 不为空订单数",
    "报价单量": "merchant_first_offer_price_time 不为空订单数",
    "驳回单量": "refuse_num>0 订单数",
    "复检单量": "recheck_num>0 订单数",
    "单均首次拍照时长": "首次拍照时长求和 / 拍照完成单量",
    "履约超时率": "签到完结时长≥30min 订单数 / 签到完结时长有效单量（剔除暂停单）",
    "拍照及时完成率": "首次拍照时长≤6min 订单数 / 拍照完成单量",
    "驳回率": "refuse_num>0 订单数 / 拍照完成单量",
    "报价成交率": "state=80 订单数 / 报价单量",
    "复检率": "recheck_num>0 订单数 / 拍照完成单量",
    "多次驳回占比": "refuse_num>1 订单数 / 驳回单量",
    "多次复检占比": "recheck_num>1 订单数 / 复检单量",
    "单均拍照报价时长": "拍照报价时长求和 / 报价单量（剔除暂停单）",
    "单均驳回次数": "refuse_num 求和 / 驳回单量",
    "单均复检次数": "recheck_num 求和 / 复检单量",
    "批量场景": "同一回收师、同一天、同一用户 UID 下的质检/成交单量≥5，记为一次批量回收；批量次均单量 = 批量单量 ÷ 批量回收次数。",
    "批量次均单量": "同一回收师、同一天、同一用户 UID 下的质检/成交单量≥5，记为一次批量回收；批量次均单量 = 批量单量 ÷ 批量回收次数。",
    "拍照报价率": "merchant_first_offer_price_time 不为空订单数 / first_photo_shot_complete_time 不为空订单数",
    "单均议价时长": "（完结时间 - merchant_first_offer_price_time）均值；分母为首次报价时间、完结时间均不为空且非暂停的订单，单位分钟",
    "单均报价次数": "merchant_offer_price_cnt 均值",
    "新人占比最高大区": "该大区新人订单量（on_work_days < 180天的订单数）/ 该大区总订单数，取占比最高的一个大区展示",
    "新人占比最高的大区": "该大区新人订单量（on_work_days < 180天的订单数）/ 该大区总订单数，取占比最高的一个大区展示",
    "驳回≥10次工程师数": "当周 refuse_num≥10 的工程师人数",
    "复检≥10次工程师数": "当周 recheck_num≥10 的工程师人数",
    "履约≥60min工程师数": "当周签到完结时长≥60min 的工程师人数",
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

RATE_METRIC_NAMES = {name for name, unit in METRICS if unit == "%"}
POSITIVE_METRIC_NAMES = {"拍照及时完成率", "报价成交率"}
ANOMALY_RULES = {
    "单均签到完结时长": {"threshold": 2.0, "suffix": "%", "reason": "时长环比波动＞2%"},
    "单均首次拍照时长": {"threshold": 0.5, "suffix": "%", "reason": "时长环比波动＞0.5%"},
    "单均拍照报价时长": {"threshold": 0.5, "suffix": "%", "reason": "时长环比波动＞0.5%"},
    "履约超时率": {"threshold": 0.5, "suffix": "pp", "reason": "环比波动＞0.5pp"},
    "拍照及时完成率": {"threshold": 0.5, "suffix": "pp", "reason": "环比波动＞0.5pp"},
    "驳回率": {"threshold": 0.5, "suffix": "pp", "reason": "环比波动＞0.5pp"},
    "复检率": {"threshold": 0.5, "suffix": "pp", "reason": "环比波动＞0.5pp"},
    "报价成交率": {"threshold": 1.0, "suffix": "pp", "reason": "环比波动＞1pp"},
    "多次驳回占比": {"threshold": 1.0, "suffix": "pp", "reason": "环比波动＞1pp"},
    "多次复检占比": {"threshold": 1.0, "suffix": "pp", "reason": "环比波动＞1pp"},
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
        "scope": (
            "offline_access "
            "mail:user_mailbox.message:readonly "
            "mail:user_mailbox.message.subject:read "
            "mail:user_mailbox.message.body:read "
            "im:message im:message.send_as_user im:resource"
        ),
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
    # 手动设为 1 仅用于本次补充授权；拿到新凭证后恢复自动刷新模式。
    set_key(".env", "FEISHU_FORCE_OAUTH", "0")
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

    for col in ["sign_time", "create_time", "cancel_time", "first_photo_shot_complete_time","merchant_first_offer_price_time",
                "merchant_last_offer_price_time","first_start_take_photo_time","finish_time",
                "first_quality_time", "quality_time"]:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")

    # 统一履约时间口径：增单从创建时刻起算；上门后取消以取消时刻作为完结。
    is_increment = pd.to_numeric(df["increment_type_id"], errors="coerce").eq(1)
    df["签到时间"] = df["sign_time"].where(~is_increment, df["create_time"])
    df["完结时间"] = df["finish_time"].where(
        df["finish_time"].notna(),
        df["cancel_time"].where(df["sign_time"].notna()),
    )
    df["是否签到"] = df["签到时间"].notna().astype(int)
    df["是否拍照"] = df["first_photo_shot_complete_time"].notna().astype(int)
    df["是否报价"] = df["merchant_first_offer_price_time"].notna().astype(int)
    df["驳回次数"] = pd.to_numeric(df["refuse_num"], errors="coerce").fillna(0)
    df["复检次数"] = pd.to_numeric(df["recheck_num"], errors="coerce").fillna(0)
    df["是否驳回"] = (df["驳回次数"] > 0).astype(int)
    df["多次驳回"] = (df["驳回次数"] > 1).astype(int)
    df["是否复检"] = (df["复检次数"] > 0).astype(int)
    df["多次复检"] = (df["复检次数"] > 1).astype(int)
    quality_time = df.get("quality_time", pd.Series(pd.NaT, index=df.index))
    first_quality_time = df.get("first_quality_time", pd.Series(pd.NaT, index=df.index))
    df["是否质检"] = (quality_time.notna() | first_quality_time.notna()).astype(int)
    df["是否暂停"] = (pd.to_numeric(df["suspended_flag"], errors="coerce") == 1).astype(int)
    df["是否增单"] = (pd.to_numeric(df["increment_type_id"], errors="coerce") == 1).astype(int)
    df["是否老人"] = (pd.to_numeric(df["on_work_days"], errors="coerce") >= SENIOR_DAYS).astype(int)
    df["是否成交"] = (pd.to_numeric(df["state"], errors="coerce") == 80).astype(int)
    df["群体"]    = df["是否老人"].map({1: "老人", 0: "新人"})

    df["首次拍照时长"] = (df["first_photo_shot_complete_time"] - df["first_start_take_photo_time"]).dt.total_seconds() / 60
    df["拍照报价时长"] = (df["merchant_last_offer_price_time"] - df["first_start_take_photo_time"]).dt.total_seconds() / 60
    df["签到完结时长"] = (df["完结时间"] - df["签到时间"]).dt.total_seconds() / 60
    df["议价时长"] = (df["完结时间"] - df["merchant_first_offer_price_time"]).dt.total_seconds() / 60
    df["报价次数"] = pd.to_numeric(df["merchant_offer_price_cnt"], errors="coerce")

    for col in ["首次拍照时长", "拍照报价时长", "签到完结时长", "议价时长"]:
        df[col] = df[col].where(df[col] >= 0, np.nan)

    iso_week = df["签到时间"].dt.isocalendar()
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
        for col in ["sign_time", "create_time", "cancel_time", "finish_time", "签到时间", "完结时间"]:
            if col in history_df.columns:
                history_df[col] = pd.to_datetime(history_df[col], errors="coerce")
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
        "签到单量":          int(len(g_ns_sign)),
        "有效签到单量":      int(len(g_sv)),
        "拍照完成单量":      int(len(g_photo)),
        "报价单量":          int(len(g_offer)),
        "驳回单量":          int(len(g_reject)),
        "复检单量":          int(len(g_recheck)),
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
    """由每周的统一签到时间推导周一至周日，用于看板展示标签。"""
    labels = {}
    sign_times = pd.to_datetime(df["签到时间"], errors="coerce")
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

# -------- 异常巡检 --------
def calc_metric_delta(values, metric_name):
    """返回最新周相对上周的变化；比例指标用 pp，时长用百分比。"""
    if len(values) < 2:
        return None
    current, previous = values[-1], values[-2]
    if current is None or previous is None:
        return None
    if metric_name in RATE_METRIC_NAMES:
        return current - previous
    if previous == 0:
        return None
    return (current - previous) / abs(previous) * 100

def is_improvement(metric_name, delta):
    if delta is None:
        return False
    return delta > 0 if metric_name in POSITIVE_METRIC_NAMES else delta < 0

def get_anomaly_events(weekly, include_normal=False):
    events = []
    for metric_name, rule in ANOMALY_RULES.items():
        values = [weekly.get(week, {}).get(metric_name) for week in sorted(weekly)]
        delta = calc_metric_delta(values, metric_name)
        if delta is None:
            continue
        if abs(delta) <= rule["threshold"] and not include_normal:
            continue
        improving = is_improvement(metric_name, delta)
        events.append({
            "metric": metric_name,
            "value": values[-1],
            "delta": round(delta, 1),
            "suffix": rule["suffix"],
            "reason": rule["reason"],
            "status": "improved" if improving else ("normal" if abs(delta) <= rule["threshold"] else "worse"),
            "direction": "波动较小" if abs(delta) <= rule["threshold"] else (("↑" if delta > 0 else "↓") + ("改善" if improving else "恶化")),
        })
    return events

def engineer_summary(g):
    """按工程师汇总异常工程师判定所需字段。"""
    if g.empty or "last_admin_name" not in g:
        return pd.DataFrame()
    rows = []
    for name, eg in g.dropna(subset=["last_admin_name"]).groupby("last_admin_name"):
        valid_duration = eg[(eg["是否暂停"] == 0) & (eg["是否签到"] == 1)]["签到完结时长"].dropna()
        refuse_count = float(eg.get("驳回次数", pd.Series(0, index=eg.index)).sum())
        recheck_count = float(eg.get("复检次数", pd.Series(0, index=eg.index)).sum())
        duration = float(valid_duration.mean()) if not valid_duration.empty else None
        hits = []
        if refuse_count >= 10: hits.append("驳回≥10次")
        if recheck_count >= 10: hits.append("复检≥10次")
        if duration is not None and duration >= 60: hits.append("单均履约时长≥60min")
        if not hits:
            continue
        group = eg["群体"].mode().iloc[0] if eg["群体"].notna().any() else "—"
        region = eg["region_name"].dropna().mode()
        fight = eg["fight_area_name"].dropna().mode()
        rows.append({
            "engineer": name, "group": group, "refuse_count": refuse_count,
            "recheck_count": recheck_count, "duration": duration,
            "region": region.iloc[0] if not region.empty else "—",
            "fight_area": fight.iloc[0] if not fight.empty else "—",
            "hits": hits,
        })
    return pd.DataFrame(rows)

def build_anomaly_data(df, weeks, overall_weekly, cate_weekly, categories):
    """构造本周异常巡检模块所需的轻量 JSON 数据。"""
    latest_week = weeks[-1]
    overall_events = get_anomaly_events(overall_weekly)

    category_events = []
    for category in categories:
        wd = cate_weekly[category]["整体"]
        events = get_anomaly_events(wd)
        if not events:
            continue
        for event in events:
            for group in ["新人", "老人"]:
                group_wd = cate_weekly[category][group]
                vals = [group_wd.get(w, {}).get(event["metric"]) for w in weeks]
                event.setdefault("segments", {})[group] = {
                    "value": vals[-1], "delta": calc_metric_delta(vals, event["metric"])
                }
        category_events.append({"category": category, "events": events})
    category_events.sort(key=lambda item: len(item["events"]), reverse=True)

    latest_df = df[df["week"] == latest_week].copy()
    latest_df["sign_date"] = pd.to_datetime(latest_df["签到时间"], errors="coerce").dt.strftime("%Y-%m-%d")
    latest_df["quality_or_deal"] = ((latest_df["是否质检"] == 1) | (latest_df["是否成交"] == 1)).astype(int)
    batch = latest_df.dropna(subset=["last_admin_name", "user_id", "sign_date"]).groupby(
        ["last_admin_name", "sign_date", "user_id"], dropna=False
    )
    batch_rows = []
    for (engineer, date, user_id), bg in batch:
        count = int(bg["quality_or_deal"].sum())
        if count < 5:
            continue
        cate = "、".join(map(str, bg["admin_quality_cate_name"].dropna().unique()[:3])) or "—"
        region = bg["region_name"].dropna().mode()
        fight = bg["fight_area_name"].dropna().mode()
        batch_rows.append({"engineer": engineer, "date": date, "user_id": str(user_id), "count": count,
                           "category": cate, "region": region.iloc[0] if not region.empty else "—",
                           "fight_area": fight.iloc[0] if not fight.empty else "—"})
    batch_rows.sort(key=lambda item: item["count"], reverse=True)

    area_rows = []
    for field, label in [("region_name", "大区"), ("fight_area_name", "战区")]:
        for area, adf in df.dropna(subset=[field]).groupby(field):
            events = get_anomaly_events(build_weekly(adf, weeks))
            for event in events:
                area_rows.append({"level": label, "name": area, **event})
    area_rows.sort(key=lambda item: (item["level"], item["name"], item["metric"]))

    engineer_trends = {}
    for label, subset in [("总体", df), ("新人", df[df["群体"] == "新人"]), ("老人", df[df["群体"] == "老人"])]:
        trend = []
        for week in weeks:
            trend.append(len(engineer_summary(subset[subset["week"] == week])))
        engineer_trends[label] = trend
    engineer_details = engineer_summary(latest_df).to_dict("records")
    engineer_details.sort(key=lambda item: (len(item["hits"]), item["refuse_count"] + item["recheck_count"]), reverse=True)

    return clean_json({
        "overall": overall_events,
        "categories": category_events,
        "batches": batch_rows,
        "batchTotal": len(batch_rows),
        "areas": area_rows,
        "engineerTrends": engineer_trends,
        "engineers": engineer_details[:100],
        "engineerTotal": len(engineer_details),
    })

def build_anomaly_drilldown(df, weeks, overall_weekly, cate_weekly, categories):
    """按触发指标构造四层递进的独立异常巡检页面数据。"""
    latest_week = weeks[-1]
    latest_df = df[df["week"] == latest_week].copy()
    total_orders = max(len(latest_df), 1)
    engineer_details = engineer_summary(latest_df).to_dict("records")
    hit_labels = ["驳回≥10次", "复检≥10次", "单均履约时长≥60min"]

    latest_df["sign_date"] = pd.to_datetime(latest_df["签到时间"], errors="coerce").dt.strftime("%Y-%m-%d")
    latest_df["quality_or_deal"] = ((latest_df["是否质检"] == 1) | (latest_df["是否成交"] == 1)).astype(int)

    # 第四层须覆盖所有品类，而不仅是前 5 个影响品类。
    # 以「同回收师、同日、同用户」的质检/成交量 >= 5 为批量场景。
    all_batch_categories, batch_engineers, batch_duration_values = [], set(), []
    for category in categories:
        cdf = latest_df[latest_df["admin_quality_cate_name"] == category]
        batches = []
        for (engineer, date, user_id), bg in cdf.dropna(
            subset=["last_admin_name", "user_id", "sign_date"]
        ).groupby(["last_admin_name", "sign_date", "user_id"]):
            count = int(bg["quality_or_deal"].sum())
            if count < 5:
                continue
            valid_duration = bg[(bg["是否暂停"] == 0) & (bg["是否签到"] == 1)]["签到完结时长"].dropna()
            avg_duration = round(float(valid_duration.mean()), 2) if not valid_duration.empty else None
            group = bg["群体"].dropna().mode()
            region = bg["region_name"].dropna().mode()
            fight_area = bg["fight_area_name"].dropna().mode()
            batches.append({
                "engineer": engineer, "date": date, "user_id": str(user_id),
                "count": count, "avg_duration": avg_duration,
                "group": group.iloc[0] if not group.empty else "—",
                "region": region.iloc[0] if not region.empty else "—",
                "fight_area": fight_area.iloc[0] if not fight_area.empty else "—",
            })
            batch_engineers.add(engineer)
            batch_duration_values.extend(valid_duration.astype(float).tolist())
        if batches:
            all_batch_categories.append({
                "category": category,
                "batches": sorted(batches, key=lambda item: item["count"], reverse=True),
            })
    all_batch_categories.sort(key=lambda item: max(row["count"] for row in item["batches"]), reverse=True)
    batch_avg_duration = (
        round(float(np.mean(batch_duration_values)), 2) if batch_duration_values else None
    )

    engineer_stats = {
        hit: {
            "整体": sum(1 for row in engineer_details if hit in row["hits"]),
            "新人": sum(1 for row in engineer_details if row["group"] == "新人" and hit in row["hits"]),
            "老人": sum(1 for row in engineer_details if row["group"] == "老人" and hit in row["hits"]),
            "批量场景": sum(1 for row in engineer_details if row["engineer"] in batch_engineers and hit in row["hits"]),
            "非批量场景": sum(1 for row in engineer_details if row["engineer"] not in batch_engineers and hit in row["hits"]),
        }
        for hit in hit_labels
    }
    metric_blocks = []
    for event in get_anomaly_events(overall_weekly, include_normal=True):
        metric = event["metric"]
        overall_current_value = overall_weekly.get(latest_week, {}).get(metric)
        impact_rows = []
        for category in categories:
            cdf = df[df["admin_quality_cate_name"] == category]
            wd = cate_weekly[category]["整体"]
            values = [wd.get(w, {}).get(metric) for w in weeks]
            delta = calc_metric_delta(values, metric)
            if values[-1] is None or delta is None:
                continue
            # Top5 候选须先满足：品类当前周指标值高于大盘当前周指标值。
            # 过滤后才按订单量权重 × 环比绝对变化幅度排序；不足 5 个不补足。
            if overall_current_value is None or values[-1] <= overall_current_value:
                continue
            current_orders = int(((cdf["week"] == latest_week)).sum())
            weight = current_orders / total_orders
            score = weight * abs(delta)
            groups = {}
            drivers = []
            for group in ["新人", "老人"]:
                gdf = cdf[cdf["群体"] == group]
                gwd = cate_weekly[category][group]
                gvals = [gwd.get(w, {}).get(metric) for w in weeks]
                gdelta = calc_metric_delta(gvals, metric)
                group_orders = int(((gdf["week"] == latest_week)).sum())
                groups[group] = {"value": gvals[-1], "delta": gdelta, "orders": group_orders}
                if gdelta is not None:
                    drivers.append((group_orders / max(current_orders, 1) * abs(gdelta), group))
            impact_rows.append({
                "category": category, "orders": current_orders, "value": values[-1], "delta": round(delta, 1),
                "suffix": event["suffix"], "score": score, "groups": groups,
                "driver": max(drivers)[1] if drivers else "—",
            })
        impact_rows.sort(key=lambda row: row["score"], reverse=True)
        top_categories = impact_rows[:5]
        score_total = sum(row["score"] for row in impact_rows) or 1
        for row in top_categories:
            row["contribution"] = round(row["score"] / score_total * 100, 1)
        metric_blocks.append({
            **event,
            "topCategories": top_categories,
            "batchCategories": all_batch_categories,
            "batchAvgDuration": batch_avg_duration,
        })

    return clean_json({
        "week": latest_week,
        "events": metric_blocks,
        "summary": {
            "metricCount": len(metric_blocks),
            "categoryCount": len({row["category"] for block in metric_blocks for row in block["topCategories"]}),
            "engineerCount": len(engineer_details),
        },
        "engineerStats": engineer_stats,
    })

# -------- 指标专属异常巡检（独立页面） --------
DRILL_RATE_METRICS = RATE_METRIC_NAMES | {"履约≥60min订单占比", "拍照报价率"}
DRILL_BAD_DOWN = {"拍照及时完成率", "报价成交率"}
DRILL_CONFIG = {
    "单均签到完结时长": {"extreme": "max", "layers": ["驳回率、复检率、单均驳回次数、单均复检次数", "履约超时率", "批量场景核查"]},
    "单均首次拍照时长": {"extreme": "max", "layers": ["新人占比最高的大区"]},
    "单均拍照报价时长": {"extreme": "max", "layers": ["驳回率、复检率、单均驳回次数、单均复检次数", "批量场景核查"]},
    "履约超时率": {"extreme": "max", "layers": ["驳回率、复检率、单均驳回次数、单均复检次数", "履约≥60min订单占比", "履约≥60min工程师数", "批量场景核查"]},
    "拍照及时完成率": {"extreme": "min", "layers": ["有变化大区的拍照及时完成率"]},
    "驳回率": {"extreme": "max", "layers": ["单均驳回次数", "驳回≥10次工程师数"]},
    "复检率": {"extreme": "max", "layers": ["单均复检次数", "复检≥10次工程师数"]},
    "报价成交率": {"extreme": "min", "layers": ["拍照报价率", "复检率", "单均议价时长", "单均报价次数"]},
    "多次驳回占比": {"extreme": "max", "layers": ["驳回≥10次工程师数", "单均签到完结时长"]},
    "多次复检占比": {"extreme": "max", "layers": ["复检≥10次工程师数", "单均签到完结时长"]},
}

def drill_calc(g):
    """补充指标专属下探所需的均值、比例指标。"""
    result = calc(g)
    photo = g[g["是否拍照"] == 1]
    # 议价时长：仅纳入已报价、已完结且非暂停订单；不限是否成交。
    offer = g[(g["是否报价"] == 1) & (g["完结时间"].notna()) & (g["是否暂停"] == 0)]
    valid_sign = g[(g["是否暂停"] == 0) & (g["是否签到"] == 1) & g["签到完结时长"].notna()]
    def avg(series):
        series = series.dropna()
        return round(float(series.mean()), 2) if not series.empty else None
    def rate(n, d):
        return round(n / d * 100, 1) if d else None
    result.update({
        "单均驳回次数": round(float(photo["驳回次数"].sum()) / len(photo), 2) if len(photo) else None,
        "单均复检次数": round(float(photo["复检次数"].sum()) / len(photo), 2) if len(photo) else None,
        "履约≥60min订单占比": rate(len(valid_sign[valid_sign["签到完结时长"] >= 60]), len(valid_sign)),
        "拍照报价率": rate(len(offer), len(photo)),
        "单均议价时长": avg(offer["议价时长"]),
        "单均报价次数": avg(offer["报价次数"]),
    })
    return result

def drill_delta(values, metric):
    if len(values) < 2 or values[-1] is None or values[-2] in (None, 0):
        return None
    return round(values[-1] - values[-2], 1) if metric in DRILL_RATE_METRICS else round((values[-1] - values[-2]) / abs(values[-2]) * 100, 1)

def drill_bad_streak(values, metric):
    """从最新周倒推连续恶化次数；订单量、批量量等非质量指标不标趋势。"""
    if metric in {"签到单量", "日均成交单量", "日均签到单", "日均报价单", "日均成交单",
                  "日均拍照完成单", "日均超时单", "日均及时完成单", "日均驳回单",
                  "日均复检单", "日均多次驳回单", "日均多次复检单"}:
        return 0
    streak = 0
    # 完成率、成交率下降为恶化；其余质量、时长、次数指标上升为恶化。
    worsens_when_down = metric in DRILL_BAD_DOWN
    for index in range(len(values) - 1, 0, -1):
        current, previous = values[index], values[index - 1]
        if current is None or previous is None or current == previous:
            break
        worsening = current < previous if worsens_when_down else current > previous
        if not worsening:
            break
        streak += 1
    return streak

def drill_value(metric, value):
    if value is None: return "—"
    if metric in DRILL_RATE_METRICS or metric in RATE_METRIC_NAMES: return f"{value}%"
    if "时长" in metric: return f"{value}min"
    return str(value)

def build_metric_drill_data(df, weeks, categories):
    """每个指标独立筛选品类，并生成总体/新人/老人三组下探数据。"""
    latest = weeks[-1]
    groups = ["总体", "新人", "老人"]
    group_df = lambda frame, group: frame if group == "总体" else frame[frame["群体"] == group]
    overall_weeks = {group: [drill_calc(group_df(df[df["week"] == w], group)) for w in weeks] for group in groups}
    latest_all = df[df["week"] == latest]
    total_orders = max(len(latest_all), 1)
    engineer_rows = engineer_summary(latest_all).to_dict("records")
    category_cache = {}
    for category in categories:
        cdf = df[df["admin_quality_cate_name"] == category]
        category_cache[category] = {
            group: [drill_calc(group_df(cdf[cdf["week"] == w], group)) for w in weeks]
            for group in groups
        }
    blocks = []
    for metric, config in DRILL_CONFIG.items():
        overall_values = [row.get(metric) for row in overall_weeks["总体"]]
        category_rows = []
        for category in categories:
            cdf = df[df["admin_quality_cate_name"] == category]
            segments = {}
            details = {}
            for group in groups:
                weekly_stats = category_cache[category][group]
                vals = [row.get(metric) for row in weekly_stats]
                segments[group] = {
                    "value": vals[-1],
                    "prev": vals[-2] if len(vals) > 1 else None,
                    "delta": drill_delta(vals, metric),
                    "prev_delta": drill_delta(vals[:-1], metric),
                    "bad_streak": drill_bad_streak(vals, metric),
                }
                details[group] = {
                    name: {
                        "value": rows[-1],
                        "prev": rows[-2] if len(rows) > 1 else None,
                        "delta": drill_delta(rows, name),
                        "prev_delta": drill_delta(rows[:-1], name),
                        "bad_streak": drill_bad_streak(rows, name),
                    }
                    for name, rows in {
                        name: [stat.get(name) for stat in weekly_stats]
                        for name in weekly_stats[-1]
                    }.items()
                }
            current = segments["总体"]["value"]
            delta = segments["总体"]["delta"]
            if current is None: continue
            weight = round(len(cdf[cdf["week"] == latest]) / total_orders * 100, 1)
            bad_direction = delta is not None and ((delta < 0) if metric in DRILL_BAD_DOWN else (delta > 0))
            category_rows.append({"category": category, "weight": weight, "segments": segments, "details": details, "bad": bad_direction})
        main = sorted([row for row in category_rows if row["bad"]], key=lambda row: row["weight"], reverse=True)[:5]
        reverse = config["extreme"] == "max"
        extreme = sorted(category_rows, key=lambda row: (row["segments"]["总体"]["value"] is not None, row["segments"]["总体"]["value"]), reverse=reverse)[:2]
        selected, selected_names = [], set()
        for source, rows in [("权重异常", main), ("极值", extreme)]:
            for row in rows:
                if row["category"] not in selected_names:
                    selected.append({**row, "source": source})
                    selected_names.add(row["category"])
        for row in selected:
            cdf = latest_all[latest_all["admin_quality_cate_name"] == row["category"]].copy()
            cdf["sign_date"] = pd.to_datetime(cdf["签到时间"], errors="coerce").dt.strftime("%Y-%m-%d")
            cdf["quality_or_deal"] = ((cdf["是否质检"] == 1) | (cdf["是否成交"] == 1)).astype(int)
            batch_stats = {}
            for group in groups:
                gdf = group_df(cdf, group)
                counts = [int(bg["quality_or_deal"].sum()) for _, bg in gdf.dropna(subset=["last_admin_name", "user_id", "sign_date"]).groupby(["last_admin_name", "sign_date", "user_id"])]
                counts = [count for count in counts if count >= 5]
                batch_stats[group] = {"次数": len(counts), "单量": sum(counts)}
            row["batch"] = batch_stats
            # 工程师异常人数必须限定在当前品类中，再按新人/老人拆分。
            # 同一工程师在该品类命中任意订单时只计 1 人。
            engineer_stats = {}
            for group in groups:
                gdf = group_df(cdf, group)
                valid_engineer = gdf["last_admin_name"].notna()
                engineer_stats[group] = {
                    "驳回≥10次工程师数": int(gdf.loc[valid_engineer & (gdf["驳回次数"] >= 10), "last_admin_name"].nunique()),
                    "复检≥10次工程师数": int(gdf.loc[valid_engineer & (gdf["复检次数"] >= 10), "last_admin_name"].nunique()),
                    "履约≥60min工程师数": int(gdf.loc[valid_engineer & (gdf["签到完结时长"] >= 60), "last_admin_name"].nunique()),
                }
            row["engineer_stats"] = engineer_stats
            # 首次拍照时长下探：按当前品类计算新人占比最高的大区。
            # 历史文件中保留的是由 on_work_days 派生的「群体」字段，
            # 因此这里以 群体 == 新人 作为 on_work_days < 180 天的等价判断。
            region_stats = []
            for region, rdf in cdf.dropna(subset=["region_name"]).groupby("region_name"):
                total_orders = len(rdf)
                newcomer_orders = int((rdf["群体"] == "新人").sum())
                if total_orders:
                    region_stats.append({
                        "name": region,
                        "newcomer_orders": newcomer_orders,
                        "total_orders": int(total_orders),
                        "share": round(newcomer_orders / total_orders * 100, 1),
                    })
            row["newbie_top_region"] = max(region_stats, key=lambda item: item["share"]) if region_stats else None
        related = []
        for label in config["layers"]:
            if "工程师数" in label:
                hit = "驳回≥10次" if "驳回" in label else ("复检≥10次" if "复检" in label else "单均履约时长≥60min")
                related.append({"type": "engineer", "title": label, "values": {g: sum(1 for row in engineer_rows if hit in row["hits"] and (g == "总体" or row["group"] == g)) for g in groups}})
            elif "批量场景" in label:
                related.append({"type": "batch", "title": label})
            elif "大区" in label:
                region_rows=[]
                for region, rdf in df.dropna(subset=["region_name"]).groupby("region_name"):
                    latest_region=rdf[rdf["week"]==latest]
                    if latest_region.empty: continue
                    if "新人占比" in label:
                        value=round((latest_region["群体"]=="新人").mean()*100,1)
                        region_rows.append({"name":region,"总体":value,"新人":value,"老人":100-value})
                    else:
                        values={g:[drill_calc(group_df(rdf[rdf["week"]==w],g)).get("拍照及时完成率") for w in weeks] for g in groups}
                        if any(drill_delta(values[g],"拍照及时完成率") not in (None,0) for g in groups):
                            region_rows.append({"name":region,**{g:values[g][-1] for g in groups}})
                related.append({"type":"region","title":label,"rows":sorted(region_rows,key=lambda x:x.get("新人",0),reverse=True)[:1] if "新人占比" in label else region_rows})
            else:
                names=[name.strip() for name in label.split("、")]
                related.append({"type":"metrics","title":label,"metrics":names})
        blocks.append({
            "metric": metric,
            "value": overall_values[-1],
            "delta": drill_delta(overall_values, metric),
            "sign_count": overall_weeks["总体"][-1].get("签到单量") if metric == "单均签到完结时长" else None,
            "sign_delta": drill_delta([row.get("签到单量") for row in overall_weeks["总体"]], "签到单量") if metric == "单均签到完结时长" else None,
            "selected": selected,
            "related": related,
        })
    return clean_json({
        "blocks": blocks,
        "latest": latest,
        "engineerStats": {
            hit: {g: sum(1 for row in engineer_rows if hit in row["hits"] and (g == "总体" or row["group"] == g)) for g in groups}
            for hit in ["驳回≥10次", "复检≥10次", "单均履约时长≥60min"]
        },
    })

# -------- 生成看板 --------
def generate_html(weeks, overall_weekly, cate_weekly, categories, week_display_labels=None, overall_groups=None, anomaly_data=None):
    week_display_labels = week_display_labels or {w: w for w in weeks}
    overall_groups = overall_groups or {}
    anomaly_data = anomaly_data or {}
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
.page-nav{{display:inline-block;margin:2px 0 18px;color:#aeb6ff;font-size:12px;text-decoration:none;border:1px solid #39436a;border-radius:6px;padding:6px 9px;background:#1a2035}}
.page-nav:hover{{color:#fff;border-color:#818cf8}}
.stitle{{font-size:18px;font-weight:700;color:#fff;letter-spacing:-.15px;margin:30px 0 14px;padding-bottom:10px;border-bottom:1px solid #2e3a5c;text-align:left}}.stitle-note{{font-size:12px;font-weight:400;color:#7b80a0;letter-spacing:0}}
.drill-heading{{margin:30px 0 14px;padding-bottom:10px;border-bottom:1px solid #2e3a5c}}.drill-heading .stitle{{margin:0;padding:0;border:0}}.drill-note{{margin-top:8px;font-size:12px;line-height:19px;color:#6370a0;font-weight:400;letter-spacing:0}}
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
.cate-table{{width:100%;border-collapse:separate;border-spacing:0;background:#222a45;border:1px solid #28334f;border-radius:12px;overflow:visible}}
.cate-table th{{background:#1a2035;color:#8890b0;font-size:12px;font-weight:600;padding:12px 10px;text-align:left;border-bottom:1px solid #2e3a5c;white-space:nowrap;position:sticky;top:0;z-index:10}}
.cate-table th.rh{{position:sticky;top:0;left:0;z-index:15;text-align:left;min-width:150px;padding-left:16px;border-right:1px solid #2e3a5c}}
.cate-table th.rh::before{{content:'';position:absolute;left:0;top:0;bottom:0;width:3px;border-radius:0 2px 2px 0;background:#6366f1}}
.cate-head-note{{margin-top:2px;color:#6370a0;font-size:10px;font-weight:400;line-height:1.2}}
.cate-table td{{border-bottom:1px solid #28334f;padding:0;vertical-align:middle;text-align:left}}
.cate-table tbody td:first-child{{position:sticky;left:0;z-index:5;background:#1a2035;border-right:1px solid #2e3a5c}}
.cate-table tbody td:first-child .rl-total,.cate-table tbody td:first-child .rl-newbie,.cate-table tbody td:first-child .rl-senior{{background:#1a2035}}
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
.anomaly-wrap{{margin:8px 0 30px;border:1px solid #2e3a5c;border-radius:12px;background:#1a1d2b;overflow:hidden}}
.anomaly-panel{{border-bottom:1px solid #28334f}}
.anomaly-panel:last-child{{border-bottom:0}}
.anomaly-toggle{{width:100%;padding:13px 16px;background:#1e2540;color:#e0e4f4;border:0;text-align:left;font-size:14px;font-weight:700;cursor:pointer;display:flex;justify-content:space-between;align-items:center}}
.anomaly-toggle:hover{{background:#222a45}}
.anomaly-body{{padding:14px 16px 16px}}
.anomaly-toggle .chev{{color:#7b80a0;font-size:12px}}
.anomaly-grid{{display:flex;gap:10px;flex-wrap:wrap}}
.anomaly-card{{min-width:185px;flex:1;background:#222a45;border:1px solid #28334f;border-left:3px solid #f87171;border-radius:8px;padding:10px 12px}}
.anomaly-card.improved{{border-left-color:#34d399}}
.anomaly-metric{{font-size:12px;color:#c4c8e0;font-weight:600;margin-bottom:6px}}
.anomaly-value{{font-size:18px;color:#fff;font-weight:700}}
.anomaly-delta{{font-size:12px;color:#ff6b6b;margin-left:6px;font-weight:600}}
.anomaly-delta.improved{{color:#4ade80}}
.anomaly-reason{{font-size:10px;color:#7b80a0;margin-top:5px}}
.anomaly-table{{width:100%;border-collapse:collapse;font-size:12px;min-width:900px}}
.anomaly-scroll{{overflow-x:auto}}
.anomaly-table th{{padding:9px 10px;color:#8890b0;background:#1a2035;border-bottom:1px solid #28334f;text-align:left;font-weight:600;white-space:nowrap}}
.anomaly-table td{{padding:9px 10px;color:#c8cce0;border-bottom:1px solid #28334f;vertical-align:top}}
.anomaly-table tr:last-child td{{border-bottom:0}}
.metric-drill-frame{{width:100%;height:1px;border:1px solid #2e3a5c;border-radius:12px;background:#161b2e;display:block}}
.tag{{display:inline-block;padding:2px 6px;margin:1px 3px 1px 0;border-radius:4px;background:#402633;color:#ff8a8a;font-size:10px;white-space:nowrap}}
.tag.good{{background:#173c35;color:#62e0ab}}
.empty-state{{color:#7b80a0;font-size:12px;padding:6px 0}}
.engineer-summary{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:14px}}
.engineer-kpi{{background:#222a45;border:1px solid #28334f;border-radius:8px;padding:10px 12px}}
.engineer-kpi .klabel{{font-size:11px;color:#8090b8}}.engineer-kpi .kvalue{{font-size:20px;font-weight:700;color:#fff;margin-top:3px}}.engineer-kpi svg{{height:30px;width:100%;display:block;margin-top:4px}}
</style>
</head>
<body>
<h1><span class="title-emoji">{title_emoji}</span>履约效率&质量看板</h1>
<div class="sub">周签到维度 · 全量 · 每周一10:00更新数据</div>
<a class="page-nav" href="anomaly.html">🚨 本周异常巡检 →</a>
<div class="stitle">数据概览（未剔除）</div>
<div id="og" class="overall-grid"></div>
<!-- 全指标品类下探统一保留在 anomaly.html（第二页）；此容器仅兼容旧脚本，不在主看板展示。 -->
<div id="main-drill" style="display:none"></div>
<div id="anomaly" class="anomaly-wrap" style="display:none"></div>
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
const AD={json.dumps(anomaly_data, ensure_ascii=False)};
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
  element.textContent=value!=null?`${{label}} ${{formatOrderCount(value)}} 单`:`${{label}} — 单`;
}}
function formatOrderCount(value){{return Math.round(Number(value));}}
function anomalyValue(metric,value){{const m=MT.find(item=>item.name===metric);return value==null?'—':`${{value}}${{m?m.unit:''}}`;}}
function anomalyDelta(event){{return `${{event.delta>0?'▲':'▼'}}${{Math.abs(event.delta).toFixed(1)}}${{event.suffix}} ${{event.direction}}`;}}
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
function showCustomFormula(name,text,e){{
  formula.innerHTML=`<strong>${{name}}</strong>${{text}}`;
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
    tw.textContent=(WL[W[ci]]||W[ci]||'')+'：';
    const displayValue=options.isOrderCount?formatOrderCount(v):v;
    tv.textContent=v!=null?`${{options.tooltipLabel?options.tooltipLabel+' ':''}}${{displayValue}}${{unit}}`:'—';
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
    hov(dailySvg,dailySvg,dailyInfo,' 单',{{tooltipLabel:dailyConfig?dailyConfig[0]:'周日均',isOrderCount:true}});
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
function anomalyPanel(title,count,content){{
  const panel=document.createElement('section');panel.className='anomaly-panel';
  const toggle=document.createElement('button');toggle.className='anomaly-toggle';toggle.innerHTML=`<span>${{title}}（${{count}}）</span><span class="chev">⌃</span>`;
  const body=document.createElement('div');body.className='anomaly-body';body.innerHTML=content;
  toggle.addEventListener('click',()=>{{const hidden=body.style.display==='none';body.style.display=hidden?'block':'none';toggle.querySelector('.chev').textContent=hidden?'⌃':'⌄';}});
  panel.append(toggle,body);return panel;
}}
function eventCard(event){{const cls=event.status==='improved'?'improved':'';return `<div class="anomaly-card ${{cls}}"><div class="anomaly-metric">${{event.metric}}</div><span class="anomaly-value">${{anomalyValue(event.metric,event.value)}}</span><span class="anomaly-delta ${{cls}}">${{anomalyDelta(event)}}</span><div class="anomaly-reason">${{event.reason}}</div></div>`;}}
const anomaly=document.getElementById('anomaly');
const overallEvents=AD.overall||[];
anomaly.appendChild(anomalyPanel('2.1 大盘异常概览',overallEvents.length,overallEvents.length?`<div class="anomaly-grid">${{overallEvents.map(eventCard).join('')}}</div>`:'<div class="empty-state">本周大盘指标均未触发异常阈值。</div>'));
const categoryRows=(AD.categories||[]).map(item=>`<tr><td><b>${{item.category}}</b></td><td>${{item.events.map(e=>`<span class="tag ${{e.status==='improved'?'good':''}}">${{e.metric}} ${{anomalyDelta(e)}}</span>`).join('')}}</td><td>${{item.events.map(e=>`${{anomalyValue(e.metric,e.value)}}`).join('<br>')}}</td><td>${{item.events.map(e=>`新人 ${{anomalyValue(e.metric,e.segments.新人.value)}} / 老人 ${{anomalyValue(e.metric,e.segments.老人.value)}}`).join('<br>')}}</td></tr>`).join('');
anomaly.appendChild(anomalyPanel('2.2 品类异常下探',(AD.categories||[]).length,categoryRows?`<div class="anomaly-scroll"><table class="anomaly-table"><thead><tr><th>品类</th><th>异常指标 / 环比</th><th>当前值</th><th>新人 / 老人拆分</th></tr></thead><tbody>${{categoryRows}}</tbody></table></div>`:'<div class="empty-state">本周各品类均未触发异常阈值。</div>'));
const batchRows=(AD.batches||[]).map(x=>`<tr><td>${{x.engineer}}</td><td>${{x.date}}</td><td>${{x.user_id}}</td><td><span class="tag">${{x.count}} 单</span></td><td>${{x.category}}</td><td>${{x.region}} / ${{x.fight_area}}</td></tr>`).join('');
anomaly.appendChild(anomalyPanel('2.3 批量质检/成交异常',AD.batchTotal||0,batchRows?`<div class="anomaly-scroll"><table class="anomaly-table"><thead><tr><th>工程师</th><th>日期</th><th>用户 UID</th><th>质检/成交单量</th><th>所属品类</th><th>大区 / 战区</th></tr></thead><tbody>${{batchRows}}</tbody></table></div>`:'<div class="empty-state">本周未发现同一回收师、同日、同用户 UID 的质检/成交单量≥5单。</div>'));
const areaRows=(AD.areas||[]).map(e=>`<tr><td>${{e.level}}</td><td>${{e.name}}</td><td>${{e.metric}}</td><td>${{anomalyValue(e.metric,e.value)}}</td><td><span class="anomaly-delta ${{e.status==='improved'?'improved':''}}">${{anomalyDelta(e)}}</span></td></tr>`).join('');
anomaly.appendChild(anomalyPanel('2.4 大区/战区异常',(AD.areas||[]).length,areaRows?`<div class="anomaly-scroll"><table class="anomaly-table"><thead><tr><th>层级</th><th>名称</th><th>异常指标</th><th>当前值</th><th>环比</th></tr></thead><tbody>${{areaRows}}</tbody></table></div>`:'<div class="empty-state">本周大区和战区均未触发异常阈值。</div>'));
const engineerRows=(AD.engineers||[]).map(x=>`<tr><td>${{x.engineer}}</td><td>${{x.group}}</td><td>${{formatOrderCount(x.refuse_count)}}</td><td>${{formatOrderCount(x.recheck_count)}}</td><td>${{x.duration==null?'—':x.duration.toFixed(2)+'min'}}</td><td>${{x.region}} / ${{x.fight_area}}</td><td>${{x.hits.map(h=>`<span class="tag">${{h}}</span>`).join('')}}</td></tr>`).join('');
const engineerContent='<div class="engineer-summary" id="engineer-summary"></div>'+(engineerRows?'<div class="anomaly-scroll"><table class="anomaly-table"><thead><tr><th>工程师</th><th>群体</th><th>驳回次数</th><th>复检次数</th><th>单均履约时长</th><th>大区 / 战区</th><th>命中条件</th></tr></thead><tbody>'+engineerRows+'</tbody></table></div>':'<div class="empty-state">本周未发现异常工程师。</div>');
const engineerPanel=anomalyPanel('2.5 异常工程师',AD.engineerTotal||0,engineerContent);
anomaly.appendChild(engineerPanel);
requestAnimationFrame(()=>{{const es=document.getElementById('engineer-summary');[['总体',CL.ov],['新人',CL.nw],['老人',CL.sr]].forEach(([label,color])=>{{const vals=(AD.engineerTrends||{{}})[label]||[];const card=document.createElement('div');card.className='engineer-kpi';card.innerHTML=`<div class="klabel">${{label}}异常工程师</div><div class="kvalue">${{vals.at(-1)||0}} 人</div>`;const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');card.appendChild(svg);es.appendChild(card);sl(svg,vals,color,30,{{strokeWidth:1.5,pointRadius:3,fillOpacity:.08}});}});}});
// 全指标品类下探：迁移至主看板整体数据概览下方。
const mainDrill=document.getElementById('main-drill');
const mdEsc=value=>String(value??'—').replace(/[&<>"]/g,char=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}}[char]));
const mdVal=(metric,value)=>{{const unit=(MT.find(item=>item.name===metric)||{{}}).unit||'';return value==null?'—':`${{value}}${{unit}}`;}};
const mdDelta=(delta,suffix)=>delta==null?'—':`${{delta>0?'▲':'▼'}}${{Math.abs(delta).toFixed(1)}}${{suffix}}`;
const mdCls=delta=>delta==null?'neutral':(delta>0?'bad':'good');
const mdContribution='计算口径：品类订单量占比 × 指标环比绝对变化，再除以全部品类该值之和。';
const mdBatch='计算口径：同一回收师、同一天、同一用户 UID 的质检/成交单量≥5，记为一次批量；批量次均单量 = 批量单量 ÷ 批量次数。';
(AD.events||[]).forEach(event=>{{
  const batchMap=Object.fromEntries((event.batchCategories||[]).map(item=>[item.category,item.batches]));
  const rows=(event.topCategories||[]).map((row,index)=>{{const batches=batchMap[row.category]||[];const volume=batches.reduce((sum,item)=>sum+item.count,0);const avg=batches.length?`${{(volume/batches.length).toFixed(1)}} 单/次（${{batches.length}}次）`:'—';return `<tr><td>Top${{index+1}} · <b>${{mdEsc(row.category)}}</b></td><td>${{row.orders}}</td><td><b>${{mdVal(event.metric,row.value)}}</b> <span class="${{mdCls(row.delta)}}">${{mdDelta(row.delta,event.suffix)}}</span></td><td>${{mdVal(event.metric,row.groups.新人.value)}} <span class="${{mdCls(row.groups.新人.delta)}}">${{mdDelta(row.groups.新人.delta,event.suffix)}}</span></td><td>${{mdVal(event.metric,row.groups.老人.value)}} <span class="${{mdCls(row.groups.老人.delta)}}">${{mdDelta(row.groups.老人.delta,event.suffix)}}</span></td><td>${{row.contribution}}%</td><td class="driver">${{mdEsc(row.driver)}}</td><td>${{avg}}</td></tr>`;}}).join('');
  const status=event.status==='normal'?'✓ 正常':'⚠️异常';
  const content=`<div class="anomaly-scroll"><table class="anomaly-table"><thead><tr><th>品类</th><th>订单量</th><th>整体（当前值 / 环比）</th><th>新人（当前值 / 环比）</th><th>老人（当前值 / 环比）</th><th>权重占比 <span class="qmark md-contribution-tip" title="查看计算口径">?</span></th><th>主要拉动方</th><th>批量次均单量 <span class="qmark md-batch-tip" title="查看计算口径">?</span></th></tr></thead><tbody>${{rows}}</tbody></table></div>`;
  const panel=anomalyPanel(`${{event.metric}} <span class="qmark md-metric-tip" title="查看计算口径">?</span> · ${{mdVal(event.metric,event.value)}} · ${{mdDelta(event.delta,event.suffix)}} ${{event.direction}} · ${{status}}`, 'Top5', content);
  panel.querySelector('.md-metric-tip').addEventListener('click',e=>{{e.stopPropagation();showFormula(event.metric,e);}});
  panel.querySelector('.md-contribution-tip').addEventListener('click',e=>{{e.stopPropagation();showCustomFormula('权重占比',mdContribution,e);}});
  panel.querySelector('.md-batch-tip').addEventListener('click',e=>{{e.stopPropagation();showCustomFormula('批量次均单量',mdBatch,e);}});
  mainDrill.appendChild(panel);
}});
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
    subscribeWeekChange(index=>{{const v=(rd.dailyDeals||[])[index];dailyDeals.textContent=v!=null?`周日均 ${{formatOrderCount(v)}} 单`:'周日均 — 单';}});
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

def generate_anomaly_html(drilldown, week_display_label):
    """生成独立异常巡检页；仅展示本周触发阈值的指标。"""
    payload = json.dumps(drilldown, ensure_ascii=False)
    html = f'''<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>本周异常巡检</title><style>
*{{box-sizing:border-box}}body{{margin:0;padding:32px 36px;min-width:1180px;background:#161b2e;color:#c8cce0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif}}
h1{{font-size:24px;color:#fff;margin:0 0 6px}}.sub{{font-size:12px;color:#7b80a0;margin-bottom:16px}}.nav{{display:inline-block;color:#aeb6ff;text-decoration:none;background:#1a2035;border:1px solid #39436a;border-radius:6px;padding:6px 9px;font-size:12px;margin-bottom:18px}}.nav:hover{{color:#fff;border-color:#818cf8}}
.summary{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:4px 0 26px}}.sum-card{{background:#1e2540;border:1px solid #2e3a5c;border-radius:12px;padding:16px;box-shadow:0 2px 8px rgba(0,0,0,.3)}}.sum-label{{font-size:12px;color:#a0aac4}}.sum-value{{font-size:28px;font-weight:700;color:#fff;margin-top:4px}}.sum-value.bad{{color:#ff6b6b}}
.metric{{background:#1a1d2b;border:1px solid #2e3a5c;border-radius:12px;margin:14px 0;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.22)}}.metric-head{{width:100%;background:#1e2540;border:0;color:#fff;padding:15px 18px;text-align:left;display:flex;align-items:center;gap:12px;font-size:16px;font-weight:700;cursor:pointer}}.metric-head:hover{{background:#222a45}}.metric-value{{font-size:18px}}.delta,.bad{{color:#ff6b6b}}.good{{color:#4ade80}}.neutral{{color:#7b80a0}}.warning{{font-size:12px;margin-left:auto;color:#ff8a8a}}.chev{{color:#7b80a0;font-size:12px}}.metric-body{{padding:14px 18px 18px}}
details{{background:#222a45;border:1px solid #28334f;border-radius:8px;margin:9px 0}}summary{{padding:11px 13px;cursor:pointer;color:#e0e4f4;font-size:13px;font-weight:700}}.layer{{padding:0 13px 13px}}.tbl-wrap{{overflow-x:auto}}table{{border-collapse:collapse;width:100%;font-size:12px}}th{{background:#1a2035;color:#8890b0;text-align:left;padding:9px 10px;white-space:nowrap}}td{{padding:9px 10px;border-bottom:1px solid #28334f;color:#c8cce0;vertical-align:top}}tr:last-child td{{border-bottom:0}}.tag{{display:inline-block;border-radius:4px;background:#402633;color:#ff8a8a;padding:2px 6px;margin:1px;font-size:10px}}.driver{{color:#ff8a8a;font-weight:700}}.ok{{color:#4ade80;font-weight:600}}.empty{{color:#7b80a0;font-size:12px;padding:5px 0}}
</style></head><body><h1>🚨 本周异常巡检</h1><div class="sub">周签到维度 · {week_display_label}</div><a class="nav" href="index.html">📊 返回主看板 ←</a><div id="summary" class="summary" style="display:none"></div><div id="metrics"></div>
<script>const D={payload};const metricUnits={{'单均签到完结时长':'min','单均拍照报价时长':'min','单均首次拍照时长':'min','履约超时率':'%','拍照及时完成率':'%','驳回率':'%','报价成交率':'%','复检率':'%','多次驳回占比':'%','多次复检占比':'%'}};
const val=(m,v)=>v==null?'—':v+metricUnits[m];const delta=e=>`${{e.delta>0?'▲':'▼'}}${{Math.abs(e.delta).toFixed(1)}}${{e.suffix}} ${{e.direction}}`;const cls=e=>e.status==='improved'?'good':'bad';
document.getElementById('summary').innerHTML=`<div class="sum-card"><div class="sum-label">触发异常指标数</div><div class="sum-value bad">${{D.summary.metricCount}}</div></div><div class="sum-card"><div class="sum-label">涉及品类数</div><div class="sum-value">${{D.summary.categoryCount}}</div></div><div class="sum-card"><div class="sum-label">异常工程师总数</div><div class="sum-value bad">${{D.summary.engineerCount}}</div></div>`;
function table(head,rows){{return `<div class="tbl-wrap"><table><thead><tr>${{head.map(x=>`<th>${{x}}</th>`).join('')}}</tr></thead><tbody>${{rows}}</tbody></table></div>`;}}
function layer(title,content){{return `<details open><summary>${{title}}</summary><div class="layer">${{content}}</div></details>`;}}
const stats=D.engineerStats||{{}};const statRows=['驳回≥10次','复检≥10次','单均履约时长≥60min'].map(k=>`<tr><td>${{k}}</td><td>${{stats[k].整体}} 人</td><td>${{stats[k].新人}} 人</td><td>${{stats[k].老人}} 人</td></tr>`).join('');
document.getElementById('metrics').innerHTML=(D.events||[]).map(e=>{{const cats=e.topCategories||[];const topRows=cats.map((x,i)=>`<tr><td>Top${{i+1}} · <b>${{x.category}}</b></td><td>${{x.orders}}</td><td>${{val(e.metric,x.value)}}</td><td class="${{x.delta>0?'bad':'good'}}">${{x.delta>0?'▲':'▼'}}${{Math.abs(x.delta).toFixed(1)}}${{x.suffix}}</td><td>${{x.contribution}}%</td></tr>`).join('');const groupRows=cats.map(x=>`<tr><td><b>${{x.category}}</b></td><td>新人：${{val(e.metric,x.groups.新人.value)}} <span class="${{x.groups.新人.delta>0?'bad':'good'}}">${{x.groups.新人.delta==null?'—':(x.groups.新人.delta>0?'▲':'▼')+Math.abs(x.groups.新人.delta).toFixed(1)+e.suffix}}</span></td><td>老人：${{val(e.metric,x.groups.老人.value)}} <span class="${{x.groups.老人.delta>0?'bad':'good'}}">${{x.groups.老人.delta==null?'—':(x.groups.老人.delta>0?'▲':'▼')+Math.abs(x.groups.老人.delta).toFixed(1)+e.suffix}}</span></td><td class="driver">主要拉动：${{x.driver}}</td></tr>`).join('');const batchContent=cats.map(x=>x.batches.length?`<tr><td rowspan="${{x.batches.length}}"><b>${{x.category}}</b></td><td>${{x.batches[0].engineer}}</td><td>${{x.batches[0].date}}</td><td>${{x.batches[0].user_id}}</td><td>${{x.batches[0].count}} 单</td></tr>${{x.batches.slice(1).map(b=>`<tr><td>${{b.engineer}}</td><td>${{b.date}}</td><td>${{b.user_id}}</td><td>${{b.count}} 单</td></tr>`).join('')}}`:`<tr><td><b>${{x.category}}</b></td><td colspan="4" class="ok">无批量场景</td></tr>`).join('');return `<section class="metric"><button class="metric-head"><span>${{e.metric}}</span><span class="metric-value">${{val(e.metric,e.value)}}</span><span class="delta ${{cls(e)}}">${{delta(e)}}</span><span class="warning">⚠️异常</span><span class="chev">⌃</span></button><div class="metric-body">${{layer('第一层：Top5 影响品类',table(['品类','订单量','当前值','环比','权重贡献度'],topRows))}}${{layer('第二层：Top5 品类内新人/老人差异',table(['品类','新人（在职<180天）','老人（在职≥180天）','主要拉动方'],groupRows))}}${{layer('第三层：异常工程师数量统计',table(['命中条件','整体','新人','老人'],statRows))}}${{layer('第四层：Top5 品类批量场景核查',table(['品类','工程师','日期','用户UID','单量'],batchContent))}}</div></section>`;}}).join('')||'<div class="empty">本周未触发异常阈值。</div>';
document.querySelectorAll('.metric-head').forEach(btn=>btn.addEventListener('click',()=>{{const body=btn.nextElementSibling;const visible=body.style.display!=='none';body.style.display=visible?'none':'block';btn.querySelector('.chev').textContent=visible?'⌄':'⌃';}}));</script></body></html>'''
    return html.replace("</body>", '''<script>
const esc = value => String(value ?? '—').replace(/[&<>"]/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[char]));
const fmtValue = (metric, value) => value == null ? '—' : `${value}${metricUnits[metric]}`;
const fmtDelta = (delta, suffix) => delta == null ? '—' : `${delta > 0 ? '▲' : '▼'}${Math.abs(delta).toFixed(1)}${suffix}`;
const deltaClass = delta => delta == null ? 'neutral' : (delta > 0 ? 'bad' : 'good');
const table2 = (head, rows) => `<div class="tbl-wrap"><table><thead><tr>${head.map(x => `<th>${x}</th>`).join('')}</tr></thead><tbody>${rows}</tbody></table></div>`;
const layer2 = (title, content) => `<details open><summary>${title}</summary><div class="layer">${content}</div></details>`;
const statRows2 = ['驳回≥10次','复检≥10次','单均履约时长≥60min'].map(hit => {
  const s = D.engineerStats?.[hit] || {};
  return `<tr><td>${hit}</td><td>${s.整体 ?? 0} 人</td><td>${s.新人 ?? 0} 人</td><td>${s.老人 ?? 0} 人</td><td>${s.批量场景 ?? 0} 人</td><td>${s.非批量场景 ?? 0} 人</td></tr>`;
}).join('');
document.getElementById('metrics').innerHTML = (D.events || []).map(event => {
  const topCategories = event.topCategories || [];
  const topRows = topCategories.map((row, index) => `<tr><td>Top${index + 1} · <b>${esc(row.category)}</b></td><td>${row.orders}</td><td>${fmtValue(event.metric,row.value)}</td><td class="${deltaClass(row.delta)}">${fmtDelta(row.delta,event.suffix)}</td><td>${row.contribution}%</td></tr>`).join('');
  const groupRows = topCategories.map(row => `<tr><td><b>${esc(row.category)}</b></td><td><b>${fmtValue(event.metric,row.value)}</b> <span class="${deltaClass(row.delta)}">${fmtDelta(row.delta,event.suffix)}</span></td><td>新人：${fmtValue(event.metric,row.groups.新人.value)} <span class="${deltaClass(row.groups.新人.delta)}">${fmtDelta(row.groups.新人.delta,event.suffix)}</span></td><td>老人：${fmtValue(event.metric,row.groups.老人.value)} <span class="${deltaClass(row.groups.老人.delta)}">${fmtDelta(row.groups.老人.delta,event.suffix)}</span></td><td class="driver">主要拉动：${esc(row.driver)}</td></tr>`).join('');
  const allBatchCategories = event.batchCategories || [];
  const batchRows = allBatchCategories.map(category => category.batches.map((batch, index) => `<tr>${index === 0 ? `<td rowspan="${category.batches.length}"><b>${esc(category.category)}</b></td>` : ''}<td>${batch.avg_duration == null ? '—' : `${batch.avg_duration}min`}</td><td>${esc(batch.engineer)}</td><td>${esc(batch.date)}</td><td>${esc(batch.user_id)}</td><td>${batch.count} 单</td></tr>`).join('')).join('') || '<tr><td colspan="6" class="ok">本周无批量场景</td></tr>';
  const batchSummary = event.batchAvgDuration == null ? '—' : `${event.batchAvgDuration}min`;
  return `<section class="metric"><button class="metric-head"><span>${esc(event.metric)}</span><span class="metric-value">${fmtValue(event.metric,event.value)}</span><span class="delta ${event.status === 'improved' ? 'good' : 'bad'}">${fmtDelta(event.delta,event.suffix)} ${esc(event.direction)}</span><span class="warning">⚠️异常</span><span class="chev">⌃</span></button><div class="metric-body">${layer2('第一层：Top5 影响品类',table2(['品类','订单量','当前值','环比','权重贡献度'],topRows))}${layer2('第二层：Top5 品类整体 / 新人 / 老人差异',table2(['品类','整体','新人（在职<180天）','老人（在职≥180天）','主要拉动方'],groupRows))}${layer2('第三层：异常工程师数量统计（按是否批量场景区分）',table2(['命中条件','整体','新人','老人','批量场景','非批量场景'],statRows2))}${layer2(`第四层：全部品类批量场景核查（批量场景单均签到履约时长：${batchSummary}）`,table2(['品类','单均签到履约时长','工程师','日期','用户UID','单量'],batchRows))}</div></section>`;
}).join('') || '<div class="empty">本周未触发异常阈值。</div>';
document.querySelectorAll('.metric-head').forEach(btn => btn.addEventListener('click', () => {
  const body = btn.nextElementSibling, visible = body.style.display !== 'none';
  body.style.display = visible ? 'none' : 'block';
  btn.querySelector('.chev').textContent = visible ? '⌄' : '⌃';
}));
</script><script>
// 第三、第四层是本周全局巡检结论，不在每个异常指标下重复呈现。
document.querySelectorAll('.metric-body details:nth-of-type(3), .metric-body details:nth-of-type(4)').forEach(node => node.remove());
const globalData = D.events?.[0] || {};
const maskUid = uid => {
  const raw = String(uid ?? '—');
  if (raw.length <= 8) return raw;
  const start = Math.max(1, Math.floor((raw.length - 8) / 2));
  return `${raw.slice(0, start)}${'*'.repeat(8)}${raw.slice(start + 8)}`;
};
const uidCell = uid => `<span class="uid-value" data-masked="${esc(maskUid(uid))}" data-full="${esc(uid)}">${esc(maskUid(uid))}</span> <button class="uid-toggle" type="button" style="margin-left:5px;padding:1px 5px;border:1px solid #39436a;border-radius:4px;background:#1a2035;color:#aeb6ff;font-size:11px;cursor:pointer">展开</button>`;
const globalBatchRows = (globalData.batchCategories || []).map(category => category.batches.map((batch, index) => `<tr>${index === 0 ? `<td rowspan="${category.batches.length}"><b>${esc(category.category)}</b></td>` : ''}<td>${batch.avg_duration == null ? '—' : `${batch.avg_duration}min`}</td><td>${esc(batch.engineer)}</td><td>${esc(batch.group)}</td><td>${esc(batch.date)}</td><td>${uidCell(batch.user_id)}</td><td>${batch.count} 单</td><td>${esc(batch.region)}</td><td>${esc(batch.fight_area)}</td></tr>`).join('')).join('') || '<tr><td colspan="9" class="ok">本周无批量场景</td></tr>';
const globalBatchAverage = globalData.batchAvgDuration == null ? '—' : `${globalData.batchAvgDuration}min`;
const globalSection = document.createElement('section');
globalSection.className = 'metric';
globalSection.innerHTML = `<div class="metric-head" style="cursor:default">全局巡检汇总（第三、四层）</div><div class="metric-body">${layer2('第三层：异常工程师数量统计（按是否批量场景区分）',table2(['命中条件','整体','新人','老人','批量场景','非批量场景'],statRows2))}${layer2(`第四层：全部品类批量场景核查（批量场景单均签到履约时长：${globalBatchAverage}）`,table2(['品类','单均签到履约时长','工程师','群体','日期','用户UID','单量','大区','战区'],globalBatchRows))}</div>`;
document.getElementById('metrics').after(globalSection);
document.querySelectorAll('.uid-toggle').forEach(button => button.addEventListener('click', () => {
  const value = button.previousElementSibling;
  const expanded = button.textContent === '收起';
  value.textContent = expanded ? value.dataset.masked : value.dataset.full;
  button.textContent = expanded ? '展开' : '收起';
}));
</script><script>
// 将第一、二层聚合成每个指标的一张完整下探表，避免信息被拆散。
const contributionDefinition = '计算口径：该品类订单量占比 × 该指标环比绝对变化，再除以全部品类该值之和；用于衡量该品类对大盘环比变化的相对贡献。';
const contributionTip = `<span style="position:relative;display:inline-block"><button class="contribution-tip" type="button" aria-expanded="false" style="width:16px;height:16px;margin-left:3px;padding:0;border:1px solid #7b80a0;border-radius:50%;background:#1a2035;color:#aeb6ff;font-size:10px;line-height:14px;cursor:pointer">?</button><span class="contribution-popover" style="display:none;position:absolute;z-index:20;left:-115px;top:22px;width:280px;padding:9px 10px;border:1px solid #3a3d52;border-radius:7px;background:#252839;color:#fff;font-size:11px;font-weight:400;line-height:1.55;white-space:normal;box-shadow:0 4px 12px rgba(0,0,0,.3)">${contributionDefinition}</span></span>`;
const batchDefinition = '计算口径：同一回收师、同一天、同一用户 UID 下的质检/成交单量≥5，记为一个批量场景；数据量为该品类全部命中批量场景的质检/成交单量之和。';
const batchTip = `<span style="position:relative;display:inline-block"><button class="batch-tip" type="button" aria-expanded="false" style="width:16px;height:16px;margin-left:3px;padding:0;border:1px solid #7b80a0;border-radius:50%;background:#1a2035;color:#aeb6ff;font-size:10px;line-height:14px;cursor:pointer">?</button><span class="batch-popover" style="display:none;position:absolute;z-index:20;right:0;top:22px;width:280px;padding:9px 10px;border:1px solid #3a3d52;border-radius:7px;background:#252839;color:#fff;font-size:11px;font-weight:400;line-height:1.55;white-space:normal;box-shadow:0 4px 12px rgba(0,0,0,.3)">${batchDefinition}</span></span>`;
const summaryEl = document.getElementById('summary');
summaryEl.style.gridTemplateColumns = 'minmax(220px, 360px)';
summaryEl.innerHTML = `<div class="sum-card"><div class="sum-label">下探指标数</div><div class="sum-value">${D.summary.metricCount}</div></div>`;
document.getElementById('metrics').innerHTML = (D.events || []).map(event => {
  const batchesByCategory = Object.fromEntries((event.batchCategories || []).map(item => [item.category, item.batches]));
  const rows = (event.topCategories || []).map((row, index) => { const batches = batchesByCategory[row.category] || []; const batchVolume = batches.reduce((total, batch) => total + batch.count, 0); return `<tr><td>Top${index + 1} · <b>${esc(row.category)}</b></td><td>${row.orders}</td><td><b>${fmtValue(event.metric,row.value)}</b> <span class="${deltaClass(row.delta)}">${fmtDelta(row.delta,event.suffix)}</span></td><td>${fmtValue(event.metric,row.groups.新人.value)} <span class="${deltaClass(row.groups.新人.delta)}">${fmtDelta(row.groups.新人.delta,event.suffix)}</span></td><td>${fmtValue(event.metric,row.groups.老人.value)} <span class="${deltaClass(row.groups.老人.delta)}">${fmtDelta(row.groups.老人.delta,event.suffix)}</span></td><td>${row.contribution}%</td><td class="driver">${esc(row.driver)}</td><td>${batchVolume ? `${batchVolume} 单（${batches.length} 次）` : '—'}</td></tr>`; }).join('');
  const body = layer2('Top5 品类影响及新人 / 老人差异汇总', table2(['品类','订单量','整体（当前值 / 环比）','新人（当前值 / 环比）','老人（当前值 / 环比）',`权重占比 ${contributionTip}`,'主要拉动方',`批量场景数据量 ${batchTip}`], rows));
  const statusClass = event.status === 'improved' ? 'good' : (event.status === 'normal' ? 'neutral' : 'bad');
  const statusText = event.status === 'normal' ? '✓ 正常' : '⚠️异常';
  const statusColor = event.status === 'improved' ? '#4ade80' : (event.status === 'normal' ? '#7b80a0' : '#ff6b6b');
  return `<section class="metric"><button class="metric-head"><span>${esc(event.metric)}</span><span class="metric-value">${fmtValue(event.metric,event.value)}</span><span class="delta ${statusClass}">${fmtDelta(event.delta,event.suffix)} ${esc(event.direction)}</span><span class="warning" style="color:${statusColor}">${statusText}</span><span class="chev">⌃</span></button><div class="metric-body">${body}</div></section>`;
}).join('') || '<div class="empty">本周未触发异常阈值。</div>';
document.querySelectorAll('#metrics .metric-head').forEach(button => button.addEventListener('click', () => {
  const body = button.nextElementSibling, visible = body.style.display !== 'none';
  body.style.display = visible ? 'none' : 'block';
  button.querySelector('.chev').textContent = visible ? '⌄' : '⌃';
}));
document.querySelectorAll('.contribution-tip').forEach(button => button.addEventListener('click', event => {
  event.stopPropagation();
  const popover = button.nextElementSibling;
  const isOpen = popover.style.display !== 'none';
  document.querySelectorAll('.contribution-popover').forEach(item => item.style.display = 'none');
  document.querySelectorAll('.contribution-tip').forEach(item => item.setAttribute('aria-expanded', 'false'));
  if (!isOpen) {
    popover.style.display = 'block';
    button.setAttribute('aria-expanded', 'true');
  }
}));
document.querySelectorAll('.batch-tip').forEach(button => button.addEventListener('click', event => {
  event.stopPropagation();
  const popover = button.nextElementSibling;
  const isOpen = popover.style.display !== 'none';
  document.querySelectorAll('.batch-popover').forEach(item => item.style.display = 'none');
  document.querySelectorAll('.batch-tip').forEach(item => item.setAttribute('aria-expanded', 'false'));
  if (!isOpen) {
    popover.style.display = 'block';
    button.setAttribute('aria-expanded', 'true');
  }
}));
</script><script>
document.getElementById('summary').style.display = 'none';
document.getElementById('metrics').innerHTML = '<div class="empty" style="padding:0 0 10px">全指标品类下探已迁移至主看板「整体数据概览」下方。</div>';
</script></body>''')

def generate_metric_anomaly_html(data, week_display_label):
    """新版：每个指标独立的异常巡检页面。"""
    payload = json.dumps(data, ensure_ascii=False)
    formulas = json.dumps(METRIC_FORMULAS, ensure_ascii=False)
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>异常巡检</title>
<style>
*{{box-sizing:border-box}}body{{margin:0;padding:32px 36px;min-width:1200px;background:#161b2e;color:#c8cce0;font-family:-apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif}}h1{{margin:0 0 6px;color:#fff;font-size:24px}}.sub{{color:#7b80a0;font-size:12px;margin-bottom:15px}}.nav{{display:inline-block;margin-bottom:18px;padding:6px 9px;border:1px solid #39436a;border-radius:6px;background:#1a2035;color:#aeb6ff;text-decoration:none;font-size:12px}}.summary{{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin:4px 0 22px}}.card,.metric{{background:#1e2540;border:1px solid #2e3a5c;border-radius:12px;box-shadow:0 2px 8px rgba(0,0,0,.25)}}.card{{padding:13px}}.card small{{color:#a0aac4}}.card b{{display:block;margin-top:4px;font-size:24px;color:#fff}}.metric{{margin:14px 0;overflow:hidden}}.metric>summary{{padding:14px 16px;background:#1a2035;color:#fff;font-size:15px;font-weight:700;cursor:pointer}}details.layer{{margin:10px 14px;background:#222a45;border:1px solid #28334f;border-radius:8px}}details.layer>summary{{padding:10px 12px;color:#e0e4f4;font-size:13px;font-weight:700;cursor:pointer}}.body{{padding:0 12px 12px;overflow:auto}}table{{border-collapse:collapse;width:100%;font-size:12px;min-width:920px}}th{{padding:9px;text-align:left;background:#1a2035;color:#8890b0;white-space:nowrap}}td{{padding:9px;border-bottom:1px solid #28334f;vertical-align:top;white-space:nowrap}}.bad{{color:#ff6b6b}}.good{{color:#4ade80}}.neutral{{color:#7b80a0}}.tag{{display:inline-block;padding:2px 5px;border-radius:4px;background:#303b60;color:#c8cce0;font-size:10px;white-space:nowrap}}.q{{display:inline-flex;align-items:center;justify-content:center;width:16px;height:16px;padding:0;border:1px solid #7b80a0;border-radius:50%;background:transparent;color:#aeb6ff;font-size:10px;cursor:pointer;margin-left:3px;vertical-align:middle}}.formula-popover{{display:none;position:fixed;z-index:9999;max-width:330px;padding:10px 12px;border:1px solid #3a4670;border-radius:8px;background:#252d4a;color:#e0e4f4;font-size:12px;line-height:1.55;box-shadow:0 8px 24px rgba(0,0,0,.35)}}.formula-popover.show{{display:block}}
body.embed{{padding:0 2px;min-width:1100px}}body.embed h1,body.embed .sub,body.embed .nav,body.embed .summary{{display:none}}
</style></head><body><h1>🚨 本周异常巡检</h1><div class="sub">周签到维度 · {week_display_label}</div><a class="nav" href="index.html">📊 返回主看板 ←</a><div id="summary" class="summary"></div><main id="app"></main><div id="formula-popover" class="formula-popover" role="dialog"></div><script>
const D={payload}, FORM={formulas};
if(new URLSearchParams(location.search).has('embed')) document.body.classList.add('embed');
const rate=new Set(['履约超时率','拍照及时完成率','驳回率','复检率','报价成交率','多次驳回占比','多次复检占比','履约≥60min订单占比','拍照报价率']);
const value=(m,v)=>v==null?'—':rate.has(m)?v+'%':m.includes('时长')?v+'min':v;
const delta=(m,v)=>v==null?'—':`${{v>0?'▲':'▼'}}${{Math.abs(v).toFixed(1)}}${{rate.has(m)?'pp':'%'}}`;
const cls=v=>v==null?'neutral':v>0?'bad':'good';
const q=(name,text)=>`<button type="button" class="q" title="${{text||FORM[name]||'暂无口径'}}" aria-label="查看${{name}}计算口径" data-formula="${{encodeURIComponent(text||FORM[name]||'暂无口径')}}">?</button>`;
const pop=document.getElementById('formula-popover');document.addEventListener('click',e=>{{const btn=e.target.closest('.q');if(!btn){{pop.classList.remove('show');return;}}e.preventDefault();e.stopPropagation();const formula=decodeURIComponent(btn.dataset.formula);pop.textContent=`口径：${{formula}}`;const r=btn.getBoundingClientRect();pop.style.left=`${{Math.min(r.left,window.innerWidth-350)}}px`;pop.style.top=`${{r.bottom+8}}px`;pop.classList.toggle('show',pop.textContent!==`口径：${{formula}}`||!pop.classList.contains('show'));}});
const triple=(row,m)=>['总体','新人','老人'].map(g=>{{const x=row.details?row.details[g][m]:row.segments[g];return `${{value(m,x?.value)}} <span class="${{cls(x?.delta)}}">${{delta(m,x?.delta)}}</span>`;}});
const table=(heads,rows)=>`<div class="body"><table><thead><tr>${{heads.map(x=>`<th>${{x}}</th>`).join('')}}</tr></thead><tbody>${{rows}}</tbody></table></div>`;
const app=document.getElementById('app'), blocks=D.blocks||[];
const bad=blocks.filter(b=>b.delta!=null && ((['拍照及时完成率','报价成交率'].includes(b.metric)&&b.delta<0)||(!['拍照及时完成率','报价成交率'].includes(b.metric)&&b.delta>0))).length;
const engineer=(hit,g)=>D.engineerStats?.[hit]?.[g]||0;
document.getElementById('summary').innerHTML=[['本周触发异常指标数',bad],['涉及异常品类数',new Set(blocks.flatMap(b=>b.selected.filter(x=>x.bad).map(x=>x.category))).size],['驳回≥10次工程师',engineer('驳回≥10次','总体')],['复检≥10次工程师',engineer('复检≥10次','总体')],['履约≥60min工程师',engineer('单均履约时长≥60min','总体')]].map(x=>`<div class="card"><small>${{x[0]}}</small><b>${{x[1]}}</b></div>`).join('');
[].forEach(b=>{{const status=b.delta==null?'无环比':((['拍照及时完成率','报价成交率'].includes(b.metric)?b.delta<0:b.delta>0)?'⚠️异常':'波动较小');const first=b.selected.map(x=>{{const s=x.segments;return `<tr><td><b>${{x.category}}</b> <span class="tag">${{x.source}}</span></td><td>${{x.weight}}%</td><td>${{value(b.metric,s.总体.value)}} <span class="${{cls(s.总体.delta)}}">${{delta(b.metric,s.总体.delta)}}</span></td><td>${{value(b.metric,s.新人.value)}} <span class="${{cls(s.新人.delta)}}">${{delta(b.metric,s.新人.delta)}}</span></td><td>${{value(b.metric,s.老人.value)}} <span class="${{cls(s.老人.delta)}}">${{delta(b.metric,s.老人.delta)}}</span></td></tr>`;}}).join('')||'<tr><td colspan="5">无符合条件品类</td></tr>';
let layers=`<details class="layer" open><summary>第一层：品类下探（权重异常 Top5 + 极值 Top2）</summary>${{table(['品类','权重占比','总体','新人（在职&lt;180天）','老人（在职≥180天）'],first)}}</details>`;
b.related.forEach((r,i)=>{{if(r.type==='metrics'){{const rows=b.selected.map(x=>r.metrics.map((m,j)=>`<tr>${{j===0?`<td rowspan="${{r.metrics.length}}"><b>${{x.category}}</b></td>`:''}}<td>${{m}} ${{q(m)}}</td><td>${{triple(x,m)[0]}}</td><td>${{triple(x,m)[1]}}</td><td>${{triple(x,m)[2]}}</td></tr>`).join('')).join('');layers+=`<details class="layer" open><summary>第${{i+2}}层：${{r.title}}</summary>${{table(['品类','指标','总体','新人','老人'],rows)}}</details>`;}}else if(r.type==='engineer'){{layers+=`<details class="layer" open><summary>第${{i+2}}层：${{r.title}}</summary>${{table(['总体','新人','老人'],`<tr><td>${{r.values.总体}} 人</td><td>${{r.values.新人}} 人</td><td>${{r.values.老人}} 人</td></tr>`)}}</details>`;}}else if(r.type==='batch'){{const rows=b.selected.map(x=>`<tr><td>${{x.category}}</td><td>${{x.batch.总体.单量}} 单 / ${{x.batch.总体.次数}} 次</td><td>${{x.batch.新人.单量}} 单 / ${{x.batch.新人.次数}} 次</td><td>${{x.batch.老人.单量}} 单 / ${{x.batch.老人.次数}} 次</td></tr>`).join('');layers+=`<details class="layer" open><summary>第${{i+2}}层：批量场景核查</summary>${{table(['品类','总体','新人','老人'],rows)}}</details>`;}}else{{const rows=(r.rows||[]).map(x=>`<tr><td>${{x.name}}</td><td>${{x.总体??'—'}}%</td><td>${{x.新人??'—'}}%</td><td>${{x.老人??'—'}}%</td></tr>`).join('');layers+=`<details class="layer" open><summary>第${{i+2}}层：${{r.title}}</summary>${{table(['大区','总体','新人','老人'],rows)}}</details>`;}}}});
app.insertAdjacentHTML('beforeend',`<details class="metric" open><summary>${{b.metric}} ${{q(b.metric)}} · ${{value(b.metric,b.value)}} · <span class="${{cls(b.delta)}}">${{delta(b.metric,b.delta)}} ${{status}}</span></summary>${{layers}}</details>`);}});
</script><script>
/* 表格矩阵视图：品类分组，整体/新人/老人三行。 */
document.head.insertAdjacentHTML('beforeend','<style>.matrix{{margin:10px 0}}.matrix .body{{padding:0;max-height:none}}.matrix th:first-child,.matrix td:first-child{{position:sticky;left:0;z-index:2}}.matrix th:first-child{{background:#1a2035}}.matrix td:first-child{{background:#1e2540}}.matrix tr.sub{{display:none;background:#181b26;font-size:11px}}.matrix tr.sub td{{color:#9ba0bc}}.matrix tr.new td:first-child{{border-left:3px solid #fb7185;color:#fb7185}}.matrix tr.old td:first-child{{border-left:3px solid #34d399;color:#34d399}}.matrix tr.total td:first-child{{border-left:3px solid #6366f1}}.matrix tr.total{{cursor:pointer}}.matrix .source{{color:#fbbf24;font-size:10px}}.matrix .extreme{{color:#7b80a0;font-size:10px}}.matrix .denom{{display:block;margin-top:3px;color:#7b80a0;font-size:10px;font-weight:400;white-space:nowrap}}.matrix .trend{{margin-left:4px;color:#f59e0b;font-size:10px;white-space:nowrap}}</style>');
const badDown2=new Set(['拍照及时完成率','报价成交率']);
const statusCell=(metric,obj)=>{{if(!obj||obj.value==null)return '<span class="neutral" title="上周：—">—</span>';const prev=obj.prev;const diff=prev==null?0:obj.value-prev;const good=badDown2.has(metric)?diff>0:diff<0;const color=diff===0?'neutral':good?'good':'bad';return `<span class="${{color}}" title="上周：${{value(metric,prev)}}">${{value(metric,obj.value)}}</span>`;}};
const matrixApp=document.getElementById('app');matrixApp.innerHTML='';
blocks.forEach(b=>{{let cols=[{{key:'main',label:b.metric}},{{key:'delta',label:'环比'}}];(b.related||[]).forEach(r=>{{if(r.type==='metrics')(r.metrics||[]).forEach(m=>cols.push({{key:m,label:m}}));else if(r.type==='engineer')cols.push({{key:'eng:'+r.title,label:r.title}});else if(r.type==='batch')cols.push({{key:'batch',label:'批量场景'}});else cols.push({{key:'region',label:r.title}});}});const rows=(b.selected||[]).map(x=>['总体','新人','老人'].map((g,i)=>{{const values=cols.map(c=>{{if(c.key==='main')return statusCell(b.metric,x.segments[g]);if(c.key==='delta')return `<span class="${{cls(x.segments[g].delta)}}" title="上周：${{value(b.metric,x.segments[g].prev)}}">${{delta(b.metric,x.segments[g].delta)}}</span>`;if(c.key.startsWith('eng:'))return `<span class="neutral">${{D.engineerStats?.[c.key.slice(4).includes('驳回')?'驳回≥10次':'复检≥10次']?.[g]??0}} 人</span>`;if(c.key==='batch'){{const z=x.batch?.[g];return `<span class="neutral" title="上周：—">${{z?z.单量+' 单 / '+z.次数+' 次':'—'}}</span>`;}}if(c.key==='region')return '<span class="neutral">—</span>';return statusCell(c.key,x.details[g][c.key]);}}).map(v=>`<td>${{v}}</td>`).join('');const label=g==='总体'?`${{x.category}}（${{x.weight}}%） <span class="${{x.source==='权重异常'?'source':'extreme'}}">${{x.source==='权重异常'?'权重Top':'极值'}}</span>`:g==='新人'?'新人（在职&lt;180天）':'老人（在职≥180天）';return `<tr class="${{i?'sub '+(g==='新人'?'new':'old'):'total'}}" data-c="${{x.category}}"><td>${{label}}</td>${{values}}</tr>`;}}).join('')).join('');const head=['品类 / 维度',...cols.map(c=>c.label)].map(x=>`<th>${{x}}</th>`).join('');matrixApp.insertAdjacentHTML('beforeend',`<details class="metric matrix" open><summary>${{b.metric}} ${{q(b.metric)}} · ${{value(b.metric,b.value)}} <span class="${{cls(b.delta)}}">${{delta(b.metric,b.delta)}}</span></summary><div class="body"><table><thead><tr>${{head}}</tr></thead><tbody>${{rows}}</tbody></table></div></details>`);}});
document.querySelectorAll('.matrix tr.total').forEach(row=>row.addEventListener('click',()=>{{const open=row.dataset.open==='1';row.dataset.open=open?'0':'1';let next=row.nextElementSibling;while(next&&next.classList.contains('sub')){{next.style.display=open?'none':'table-row';next=next.nextElementSibling;}}}}));
document.querySelectorAll('.matrix').forEach(matrix=>{{let rank=0;matrix.querySelectorAll('tr.total .source').forEach(tag=>{{tag.textContent=`权重Top${{++rank}}`;}});}});
document.querySelectorAll('.matrix').forEach((matrix,index)=>{{
  const b=blocks[index], table=matrix.querySelector('table'), head=table.rows[0];
  const deltaIndex=[...head.cells].findIndex(cell=>cell.textContent.trim()==='环比');
  if(deltaIndex>=0){{head.appendChild(head.cells[deltaIndex]);[...table.tBodies[0].rows].forEach(row=>row.appendChild(row.cells[deltaIndex]));}}
  if(b.metric==='单均签到完结时长'){{
    const metricHead=head.cells[1], countHead=document.createElement('th');countHead.textContent='签到单量';metricHead.after(countHead);
    [...table.tBodies[0].rows].forEach(row=>{{const group=row.classList.contains('new')?'新人':row.classList.contains('old')?'老人':'总体';const item=(b.selected||[]).find(x=>x.category===row.dataset.c);const count=item?.details?.[group]?.['签到单量']?.value;const cell=document.createElement('td');cell.className='neutral';cell.textContent=count==null?'—':Number(count).toLocaleString()+' 单';row.cells[1].after(cell);}});
    const summary=matrix.querySelector('summary'), deltaEl=summary.querySelector(':scope > span');const ring=deltaEl?.textContent||'—';if(deltaEl)deltaEl.remove();summary.append(` · 签到单量 ${{Number(b.sign_count||0).toLocaleString()}} 单（环比 ${{ring}}）`);
  }}
}});
document.querySelectorAll('.matrix th').forEach(th=>{{const name=th.textContent.trim();if(name!=='品类 / 维度'&&name!=='环比'&&!th.querySelector('.q'))th.insertAdjacentHTML('beforeend',q(name));}});
</script><script>
/* 指标值与环比成对排列；该脚本覆盖旧矩阵，确保每列紧跟对应环比。 */
(() => {{
  const app=document.getElementById('app'); app.innerHTML='';
  const positive=new Set(['拍照及时完成率','报价成交率']);
  const denominatorByMetric={{'单均签到完结时长':'有效签到单量','单均拍照报价时长':'报价单量','单均首次拍照时长':'拍照完成单量','履约超时率':'有效签到单量','拍照及时完成率':'拍照完成单量','驳回率':'拍照完成单量','报价成交率':'报价单量','复检率':'拍照完成单量','多次驳回占比':'驳回单量','多次复检占比':'复检单量'}};
  const fmt=(name,value)=>{{if(value==null)return '—';if(name==='批量场景'||name==='批量次均单量')return value;if(name.endsWith('单量'))return Number(value).toLocaleString()+' 单';if(name.includes('工程师数'))return value+' 人';if(rate.has(name))return value+'%';if(name.includes('时长'))return value+'min';return String(value);}};
  const dFmt=(name,value)=>{{if(value==null)return '—';return `${{value>0?'▲':'▼'}}${{Math.abs(value).toFixed(1)}}${{rate.has(name)?'pp':'%'}}`;}};
  const state=(name,value,prev)=>{{if(value==null||prev==null||value===prev)return 'neutral';const better=positive.has(name)?value>prev:value<prev;return better?'good':'bad';}};
  const valueCell=(name,obj)=>{{const previous=obj?.prev;const current=obj?.value;return `<td title="上周：${{fmt(name,previous)}}"><span class="${{state(name,current,previous)}}">${{fmt(name,current)}}</span></td>`;}};
  const deltaCell=(name,obj)=>{{const previousDelta=obj?.prev_delta;return `<td title="上周环比：${{dFmt(name,previousDelta)}}"><span class="${{state(name,obj?.value,obj?.prev)}}">${{dFmt(name,obj?.delta)}}</span></td>`;}};
  const combinedCell=(name,obj)=>{{const previous=obj?.prev,current=obj?.value,change=obj?.delta;const deltaText=change==null?'':`（<span class="${{state(name,current,previous)}}">${{dFmt(name,change)}}</span>）`;const trend=obj?.bad_streak>=2?`<span class="trend">「连续${{obj.bad_streak}}周${{positive.has(name)?'↓':'↑'}}」</span>`:'';return `<td title="上周：${{fmt(name,previous)}}"><span class="${{state(name,current,previous)}}">${{fmt(name,current)}}</span>${{deltaText}}${{trend}}</td>`;}};
  const buildColumns=b=>{{const cols=[{{key:'main',name:b.metric}}];(b.related||[]).forEach(r=>{{if(r.type==='metrics')(r.metrics||[]).forEach(m=>cols.push({{key:'metric:'+m,name:m}}));else if(r.type==='engineer')cols.push({{key:'engineer:'+r.title,name:r.title}});else if(r.type==='batch')cols.push({{key:'batch',name:'批量次均单量'}});else cols.push({{key:'region:'+r.title,name:r.title}});}});return cols;}};
  blocks.forEach(b=>{{
    const cols=buildColumns(b);
    const header='<th>品类 / 维度</th>'+cols.map(c=>`<th>${{c.name}} ${{q(c.name)}}</th>`).join('');
    const dataFor=(row,group,col)=>{{
      if(col.key==='main')return row.segments?.[group];
      if(col.key.startsWith('metric:'))return row.details?.[group]?.[col.name];
      if(col.key.startsWith('engineer:'))return {{value:row.engineer_stats?.[group]?.[col.name]??0,prev:null,delta:null,prev_delta:null}};
      if(col.key==='batch'){{
        const item=row.batch?.[group];
        const volume=Number(item?.单量), times=Number(item?.次数);
        // 历史 JSON 中缺失或非数值的批量字段不参与计算，避免页面出现 NaN。
        const display=Number.isFinite(volume)&&Number.isFinite(times)&&times>0
          ? `${{(volume/times).toFixed(1)}} 单/次（${{times}} 次）`
          : null;
        return {{value:display,prev:null,delta:null,prev_delta:null}};
      }}
      if(col.key.startsWith('region:')){{
        const item=row.newbie_top_region;
        const display=item?`${{item.name}} ${{item.share}}%（${{item.newcomer_orders}}/${{item.total_orders}}单）`:null;
        return {{value:display,prev:null,delta:null,prev_delta:null}};
      }}
      return {{value:null,prev:null,delta:null,prev_delta:null}};
    }};
    const rows=(b.selected||[]).map(row=>['总体','新人','老人'].map((group,index)=>{{
      const denominator=denominatorByMetric[b.metric];
      const denominatorData=denominator?row.details?.[group]?.[denominator]:null;
      const denominatorNote=denominatorData?.value!=null?`<span class="denom">${{denominator}} ${{fmt(denominator,denominatorData.value)}}（${{dFmt(denominator,denominatorData.delta)}}）</span>`:'';
      const label=group==='总体'?`<span class="${{row.source==='权重异常'?'source':'extreme'}}">${{row.source==='权重异常'?'权重Top':'极值'}}</span> ${{row.category}}（${{row.weight}}%）${{denominatorNote}}`:(group==='新人'?`新人（在职&lt;180天）${{denominatorNote}}`:`老人（在职≥180天）${{denominatorNote}}`);
      const cells=cols.map(col=>{{const obj=dataFor(row,group,col);return combinedCell(col.name,obj);}}).join('');
      return `<tr class="${{index?'sub '+(group==='新人'?'new':'old'):'total'}}" data-c="${{row.category}}"><td>${{label}}</td>${{cells}}</tr>`;
    }}).join('')).join('');
    app.insertAdjacentHTML('beforeend',`<details class="metric matrix"><summary>${{b.metric}} ${{q(b.metric)}} · ${{fmt(b.metric,b.value)}} <span class="${{state(b.metric,b.value,b.value-(b.delta||0))}}">${{dFmt(b.metric,b.delta)}}</span></summary><div class="body"><table><thead><tr>${{header}}</tr></thead><tbody>${{rows}}</tbody></table></div></details>`);
  }});
  document.querySelectorAll('.matrix tr.total').forEach(row=>row.addEventListener('click',()=>{{const opened=row.dataset.open==='1';row.dataset.open=opened?'0':'1';let next=row.nextElementSibling;while(next&&next.classList.contains('sub')){{next.style.display=opened?'none':'table-row';next=next.nextElementSibling;}}}}));
  document.querySelectorAll('.matrix').forEach(matrix=>{{let rank=0;matrix.querySelectorAll('tr.total .source').forEach(tag=>tag.textContent=`权重Top${{++rank}}`);}});
}})();
</script></body></html>'''

def send_feishu_notification(latest_week_label, html_path, page_name="履约效率&质量看板", emoji="📊"):
    """以应用机器人身份向指定企业邮箱发送看板更新文字和 HTML 附件。"""
    # 不能以用户身份向自己发消息：飞书会接受请求，但通常不会生成可见会话。
    # 机器人身份发送才会形成可见的应用单聊通知。
    bot_token = get_token()
    headers = {"Authorization": f"Bearer {bot_token}"}

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    text = (
        f"{emoji} {page_name}已更新\n"
        f"本周数据：{latest_week_label}\n"
        f"生成时间：{generated_at}\n"
        "请下载附件用浏览器打开查看完整看板 👇"
    )
    message_url = "https://open.feishu.cn/open-apis/im/v1/messages"
    text_response = requests.post(
        message_url,
        headers={**headers, "Content-Type": "application/json; charset=utf-8"},
        params={"receive_id_type": "email"},
        json={
            "receive_id": NOTIFICATION_RECEIVER_EMAIL,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        },
        timeout=30,
    ).json()
    if text_response.get("code") != 0:
        raise Exception(f"飞书文字消息发送失败: {text_response.get('msg')}")

    with html_path.open("rb") as html_file:
        upload_response = requests.post(
            "https://open.feishu.cn/open-apis/im/v1/files",
            headers=headers,
            data={"file_type": "stream", "file_name": "index.html"},
            files={"file": ("index.html", html_file, "text/html")},
            timeout=120,
        ).json()
    if upload_response.get("code") != 0:
        raise Exception(f"飞书附件上传失败: {upload_response.get('msg')}")
    file_key = upload_response.get("data", {}).get("file_key")
    if not file_key:
        raise Exception("飞书附件上传失败: 响应中缺少 file_key")

    file_response = requests.post(
        message_url,
        headers={**headers, "Content-Type": "application/json; charset=utf-8"},
        params={"receive_id_type": "email"},
        json={
            "receive_id": NOTIFICATION_RECEIVER_EMAIL,
            "msg_type": "file",
            "content": json.dumps({"file_key": file_key}),
        },
        timeout=30,
    ).json()
    if file_response.get("code") != 0:
        raise Exception(f"飞书文件消息发送失败: {file_response.get('msg')}")
    print("✅ 飞书消息已发送")

def push_to_github():
    """提交并推送首页看板；本地未配置 Git 时不影响看板生成。"""
    if not (PROJECT_DIR / ".git").exists():
        print("⚠️ 未初始化 Git 仓库，跳过 GitHub Pages 推送")
        return
    try:
        subprocess.run(["git", "add", "index.html", "anomaly.html"], cwd=PROJECT_DIR, check=True)
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

    anomaly_drilldown = build_anomaly_drilldown(df, weeks, overall_weekly, cate_weekly, list(categories))
    metric_drilldown = build_metric_drill_data(df, weeks, list(categories))
    overall_weekly = clean_json(overall_weekly)
    overall_groups = clean_json(overall_groups)
    cate_weekly    = clean_json(cate_weekly)

    print("🎨 生成看板...")
    week_display_labels = build_week_display_labels(df, weeks)
    html = generate_html(
        weeks, overall_weekly, cate_weekly, list(categories), week_display_labels,
        overall_groups, anomaly_drilldown,
    )
    anomaly_html = generate_metric_anomaly_html(metric_drilldown, week_display_labels.get(latest_week, latest_week))

    OUTPUT_DIR.mkdir(exist_ok=True)
    out = OUTPUT_DIR / f"履约效率_质量看板_{datetime.now().strftime('%Y%m%d')}.html"
    anomaly_out = OUTPUT_DIR / f"异常巡检_{datetime.now().strftime('%Y%m%d')}.html"
    out.write_text(html, encoding="utf-8")
    anomaly_out.write_text(anomaly_html, encoding="utf-8")
    shutil.copyfile(out, PUBLISH_PATH)
    shutil.copyfile(anomaly_out, PROJECT_DIR / "anomaly.html")
    print(f"🌐 Pages 首页已更新：{PUBLISH_PATH}")
    push_to_github()
    try:
        send_feishu_notification(week_display_labels.get(latest_week, latest_week), PUBLISH_PATH)
        send_feishu_notification(week_display_labels.get(latest_week, latest_week), PROJECT_DIR / "anomaly.html", "本周异常巡检", "🚨")
    except Exception as exc:
        # 看板已生成时，消息失败不应让 cron 误判整次数据处理失败。
        print(f"⚠️ 飞书消息发送失败，不影响看板生成：{exc}")

    print(f"\n✅ 完成！看板路径：{out.resolve()}")

if __name__ == "__main__":
    if "--authorize" in sys.argv:
        # 仅用于权限新增后的安全补充授权，不读取邮件或改写历史数据。
        os.environ["FEISHU_FORCE_OAUTH"] = "1"
        get_user_token()
    else:
        main()
