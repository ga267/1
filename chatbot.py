"""飞书履约数据问答机器人。

启动：
    .venv/bin/pip install -r requirements-chatbot.txt
    .venv/bin/python chatbot.py

然后将 ngrok 暴露的 /feishu/events 地址配置到飞书开放平台的事件订阅，
订阅事件选择「接收消息 v2.0（im.message.receive_v1）」。
"""

import json
import logging
from numbers import Real
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
import uvicorn

try:
    from zhipuai import ZhipuAI
except ModuleNotFoundError:  # 方便在未安装依赖时给出明确提示。
    ZhipuAI = None

import agent


PROJECT_DIR = Path(__file__).resolve().parent
HISTORY_PATH = PROJECT_DIR / "dashboard_output" / "history_data.json.gz"

load_dotenv(PROJECT_DIR / ".env")

APP_ID = os.getenv("FEISHU_APP_ID")
APP_SECRET = os.getenv("FEISHU_APP_SECRET")
VERIFICATION_TOKEN = os.getenv("FEISHU_VERIFICATION_TOKEN", "")
ZHIPUAI_API_KEY = os.getenv("ZHIPUAI_API_KEY")
ZHIPUAI_MODEL = os.getenv("ZHIPUAI_MODEL", "glm-4-flash")
HOST = os.getenv("CHATBOT_HOST", "127.0.0.1")
PORT = int(os.getenv("CHATBOT_PORT", "8080"))

SYSTEM_PROMPT = """你是一个履约效率数据分析助手，负责解读回收师拍照履约数据看板。
你掌握近6周大盘整体指标（含新人/老人拆分）、近6周各品类指标（含新人/老人拆分）和本周异常摘要。

核心指标口径：
- 驳回率：refuse_num>0订单数 / 拍照完成单量
- 复检率：recheck_num>0订单数 / 拍照完成单量
- 拍照及时完成率：首次拍照时长≤6min订单数 / 拍照完成单量
- 履约超时率：签到完结时长≥30min订单数 / 有效签到单量（剔除暂停单）
- 报价成交率：state=80订单数 / 报价单量
- 新人：在职<180天；老人：在职≥180天

回答要求：
1. 直接给出结论，不说无关内容。
2. 百分比、时长等指标保留小数点后一位；单量为整数。
3. 涉及异常时，指出可能拉动的品类或新人/老人群体（仅限上下文中的证据）。
4. 问题超出数据范围时，直接回答“没有该数据”。
5. 不得编造数据、字段、原因或用户信息。
"""

app = FastAPI(title="履约效率数据问答机器人")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("fulfillment-chatbot")

_context_cache: dict[str, Any] = {"mtime": None, "value": None}
_cache_lock = threading.Lock()
_seen_messages: dict[str, float] = {}
_seen_lock = threading.Lock()


def _to_datetime(df: pd.DataFrame) -> pd.DataFrame:
    """恢复压缩历史数据中的日期列，便于复用看板聚合逻辑。"""
    for column in ["sign_time", "create_time", "cancel_time", "finish_time", "签到时间", "完结时间"]:
        if column in df.columns:
            df[column] = pd.to_datetime(df[column], errors="coerce")
    return df


def _latest_categories(df: pd.DataFrame, latest_week: str) -> list[str]:
    latest = df[df["week"] == latest_week].dropna(subset=["admin_quality_cate_name"])
    return list(
        latest.groupby("admin_quality_cate_name")["是否成交"]
        .sum()
        .sort_values(ascending=False)
        .index
    )


def _number(value: Any) -> Any:
    """让 numpy 标量和 NaN 可安全序列化。"""
    if value is None or pd.isna(value):
        return None
    if isinstance(value, Real):
        return round(float(value), 2)
    return value


def _compact_weekly(weekly: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        week: {metric: _number(value) for metric, value in values.items()}
        for week, values in weekly.items()
    }


def build_dashboard_context() -> dict[str, Any]:
    """把本地订单明细压缩为问题回答所需的聚合上下文。

    不向模型发送订单级数据、用户 UID 或工程师姓名，避免敏感数据外发且保持请求体稳定。
    """
    if not HISTORY_PATH.exists():
        raise FileNotFoundError(f"未找到历史数据：{HISTORY_PATH}")

    mtime = HISTORY_PATH.stat().st_mtime
    with _cache_lock:
        if _context_cache["mtime"] == mtime and _context_cache["value"] is not None:
            return _context_cache["value"]

        df = _to_datetime(pd.read_json(HISTORY_PATH, orient="records", compression="gzip"))
        weeks = sorted(df["week"].dropna().astype(str).unique())
        if not weeks:
            raise ValueError("历史数据中没有有效周标签")
        latest_week = weeks[-1]
        categories = _latest_categories(df, latest_week)

        overall = agent.build_weekly(df, weeks)
        groups = {
            "新人": agent.build_weekly(df[df["群体"] == "新人"], weeks),
            "老人": agent.build_weekly(df[df["群体"] == "老人"], weeks),
        }
        category_weekly: dict[str, dict[str, dict[str, dict[str, Any]]]] = {}
        for category in categories:
            cdf = df[df["admin_quality_cate_name"] == category]
            category_weekly[category] = {
                "整体": agent.build_weekly(cdf, weeks),
                "新人": agent.build_weekly(cdf[cdf["群体"] == "新人"], weeks),
                "老人": agent.build_weekly(cdf[cdf["群体"] == "老人"], weeks),
            }

        anomaly = agent.build_anomaly_data(df, weeks, overall, category_weekly, categories)
        context = {
            "data_updated_at": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M"),
            "weeks": weeks,
            "latest_week": latest_week,
            "formulas": agent.METRIC_FORMULAS,
            "overall_weekly": _compact_weekly(overall),
            "group_weekly": {group: _compact_weekly(values) for group, values in groups.items()},
            "category_weekly": {
                category: {group: _compact_weekly(values) for group, values in groups.items()}
                for category, groups in category_weekly.items()
            },
            "latest_anomalies": {
                "overall": anomaly.get("overall", []),
                "categories": anomaly.get("categories", []),
            },
        }
        _context_cache.update(mtime=mtime, value=context)
        return context


def call_zhipuai(question: str) -> str:
    if ZhipuAI is None:
        raise RuntimeError("未安装 zhipuai，请先执行 .venv/bin/pip install -r requirements-chatbot.txt")
    if not ZHIPUAI_API_KEY:
        raise RuntimeError("未配置 ZHIPUAI_API_KEY，无法调用智谱 API")

    context = build_dashboard_context()
    user_message = (
        "当前聚合数据：\n"
        + json.dumps(context, ensure_ascii=False, separators=(",", ":"))
        + f"\n\n用户问题：{question}"
    )
    try:
        client = ZhipuAI(api_key=ZHIPUAI_API_KEY)
        response = client.chat.completions.create(
            model=ZHIPUAI_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
        )
    except Exception as exc:
        logger.error("智谱 API 调用失败：%s", exc)
        raise RuntimeError("智谱 API 调用失败，请稍后重试") from exc
    answer = (response.choices[0].message.content or "").strip()
    return answer or "没有生成可用回答。"


def tenant_access_token() -> str:
    response = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": APP_ID, "app_secret": APP_SECRET},
        timeout=30,
    ).json()
    if response.get("code") != 0:
        raise RuntimeError(f"飞书 tenant_access_token 获取失败：{response.get('msg')}")
    return response["tenant_access_token"]


def send_text_reply(chat_id: str, text: str) -> None:
    """向原会话发纯文本回复。

    飞书 text 消息不解析 Markdown 加粗，因此这里保持纯文本，避免用户看到 ** 符号。
    """
    response = requests.post(
        "https://open.feishu.cn/open-apis/im/v1/messages",
        headers={
            "Authorization": f"Bearer {tenant_access_token()}",
            "Content-Type": "application/json; charset=utf-8",
        },
        params={"receive_id_type": "chat_id"},
        json={
            "receive_id": chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        },
        timeout=30,
    ).json()
    if response.get("code") != 0:
        raise RuntimeError(f"飞书消息发送失败：{response.get('msg')}")


def answer_and_reply(question: str, chat_id: str) -> None:
    """在后台处理模型请求，避免飞书因回调超时而重复投递事件。"""
    try:
        answer = call_zhipuai(question)
    except Exception as exc:
        logger.exception("问答处理失败")
        answer = f"暂时无法回答：{exc}"
    try:
        send_text_reply(chat_id, answer)
    except Exception:
        logger.exception("飞书回复发送失败")


def _already_seen(message_id: str) -> bool:
    now = time.time()
    with _seen_lock:
        # 飞书可能重试事件；10 分钟内的同一消息只处理一次。
        for key, timestamp in list(_seen_messages.items()):
            if now - timestamp > 600:
                _seen_messages.pop(key, None)
        if message_id in _seen_messages:
            return True
        _seen_messages[message_id] = now
        return False


def _event_token_valid(payload: dict[str, Any]) -> bool:
    """未配置 token 时允许 URL 验证；正式环境建议在 .env 配置同一个 token。"""
    if not VERIFICATION_TOKEN:
        return True
    header = payload.get("header", {})
    return payload.get("token") == VERIFICATION_TOKEN or header.get("token") == VERIFICATION_TOKEN


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "history_exists": HISTORY_PATH.exists()}


@app.post("/feishu/events")
async def feishu_events(request: Request) -> JSONResponse:
    payload = await request.json()

    # 飞书保存 Request URL 时会首先发送 URL 验证挑战。
    if payload.get("type") == "url_verification":
        if not _event_token_valid(payload):
            raise HTTPException(status_code=403, detail="verification token 不匹配")
        return JSONResponse({"challenge": payload.get("challenge", "")})

    if not _event_token_valid(payload):
        raise HTTPException(status_code=403, detail="verification token 不匹配")

    header = payload.get("header", {})
    if header.get("event_type") != "im.message.receive_v1":
        return JSONResponse({"code": 0})

    event = payload.get("event", {})
    message = event.get("message", {})
    if message.get("message_type") != "text":
        return JSONResponse({"code": 0})
    message_id = message.get("message_id", "")
    if message_id and _already_seen(message_id):
        return JSONResponse({"code": 0})

    try:
        question = json.loads(message.get("content", "{}")).get("text", "").strip()
    except json.JSONDecodeError:
        question = ""
    chat_id = message.get("chat_id")
    if not question or not chat_id:
        return JSONResponse({"code": 0})

    # 飞书事件回调需及时返回 2xx；模型调用在后台完成后再回复原会话。
    threading.Thread(target=answer_and_reply, args=(question[:2000], chat_id), daemon=True).start()
    return JSONResponse({"code": 0})


if __name__ == "__main__":
    missing = [name for name, value in {
        "FEISHU_APP_ID": APP_ID,
        "FEISHU_APP_SECRET": APP_SECRET,
        "ZHIPUAI_API_KEY": ZHIPUAI_API_KEY,
    }.items() if not value]
    if missing:
        raise SystemExit("缺少配置：" + ", ".join(missing))
    logger.info("问答服务启动：POST /feishu/events，健康检查：GET /health")
    uvicorn.run(app, host=HOST, port=PORT)
