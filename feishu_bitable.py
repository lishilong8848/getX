"""飞书多维表格（Bitable）客户端。

封装：tenant_access_token 缓存、按推文 ID 查重、新增记录。
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone, timedelta
from typing import Any

import requests


BASE = "https://open.feishu.cn/open-apis"
DEFAULT_TIMEOUT = 15
TOKEN_TTL_SECONDS = 60 * 90  # tenant_access_token 有效期 2 小时；保守 90 分钟刷新一次

# 飞书 Bitable 返回的 token 相关错误码（用于触发强制刷新重试）
_TOKEN_INVALID_CODES = {
    99991663,  # token invalid
    99991664,  # token expired
    99991668,  # invalid access token
    99991669,  # access token expired
    230001,    # token 缺失/格式错
    230002,    # token 无效
    230020,    # token 已过期
}


class FeishuBitable:
    """飞书 Bitable 客户端，负责鉴权、查重、写记录。"""

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        app_token: str,
        table_id: str,
        tweet_id_field: str = "推文ID",
        base: str = BASE,
        timeout: int = DEFAULT_TIMEOUT,
    ):
        self.app_id = app_id
        self.app_secret = app_secret
        self.app_token = app_token
        self.table_id = table_id
        self.tweet_id_field = tweet_id_field
        self.base = base.rstrip("/")
        self.timeout = timeout
        self._token: str | None = None
        self._token_expire_at: float = 0.0
        self._field_id_map: dict[str, str] | None = None

    # ---------------- 鉴权 ----------------
    def _refresh_token(self) -> str:
        """强制刷新 token（忽略缓存）。"""
        r = requests.post(
            f"{self.base}/auth/v3/tenant_access_token/internal",
            json={"app_id": self.app_id, "app_secret": self.app_secret},
            timeout=self.timeout,
        )
        data = r.json()
        if data.get("code") != 0:
            raise RuntimeError(f"获取 tenant_access_token 失败: {data}")
        self._token = data["tenant_access_token"]
        self._token_expire_at = time.time() + TOKEN_TTL_SECONDS
        return self._token

    def _ensure_token(self) -> str:
        if self._token and time.time() < self._token_expire_at:
            return self._token
        return self._refresh_token()

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._ensure_token()}",
            "Content-Type": "application/json; charset=utf-8",
        }

    def _is_token_error(self, data: dict) -> bool:
        """判断响应是否属于 token 失效/过期。"""
        code = data.get("code")
        if code in _TOKEN_INVALID_CODES:
            return True
        msg = (data.get("msg") or "").lower()
        return ("access token" in msg and ("invalid" in msg or "expired" in msg)) or \
               ("invalid access token" in msg)

    def _request_with_token_retry(self, method: str, url: str, *, params=None, json_body=None, max_retries: int = 1):
        """统一请求方法：遇 token 失效错误强制刷新后再重试一次。"""
        for attempt in range(max_retries + 1):
            resp = requests.request(method, url, params=params, json=json_body,
                                    headers=self._headers(), timeout=self.timeout)
            try:
                data = resp.json()
            except Exception:
                resp.raise_for_status()
                return resp
            if self._is_token_error(data) and attempt < max_retries:
                # 强制刷新 token 再试
                self._refresh_token()
                continue
            return resp

    # ---------------- 字段 ----------------
    def get_field_id_map(self, refresh: bool = False) -> dict[str, str]:
        """获取 {字段名: field_id} 映射。缓存到内存。"""
        if self._field_id_map is not None and not refresh:
            return self._field_id_map
        r = self._request_with_token_retry(
            "GET",
            f"{self.base}/bitable/v1/apps/{self.app_token}/tables/{self.table_id}/fields",
        )
        data = r.json()
        if data.get("code") != 0:
            raise RuntimeError(f"获取字段失败: {data}")
        self._field_id_map = {item["field_name"]: item["field_id"] for item in data["data"]["items"]}
        return self._field_id_map

    # ---------------- 查重 ----------------
    def record_exists(self, tweet_id: str) -> bool:
        """按推文ID判断记录是否已存在。

        实现：用 list 记录接口分页扫描（每页 500 条，最多 5000 条）。
        Feishu Bitable 的 search 接口需要额外的 base/table 权限配置（99992402），
        这里只用 list 接口，足够覆盖每天 ≤10 条、年内 ≤5000 条的监控量。
        """
        field_map = self.get_field_id_map()
        if self.tweet_id_field not in field_map:
            raise RuntimeError(f"找不到字段: {self.tweet_id_field}")
        return self._exists_via_list(tweet_id)

    def _exists_via_list(self, tweet_id: str) -> bool:
        """用 list 记录接口逐条匹配（list 接口的 fields 字典用字段名做 key）。"""
        url = f"{self.base}/bitable/v1/apps/{self.app_token}/tables/{self.table_id}/records"
        page_token = None
        scanned = 0
        target = str(tweet_id)
        while True:
            params: dict[str, Any] = {"page_size": 500, "automatic_fields": "false"}
            if page_token:
                params["page_token"] = page_token
            r = self._request_with_token_retry("GET", url, params=params)
            data = r.json()
            if data.get("code") != 0:
                raise RuntimeError(f"列出记录失败: {data}")
            items = data["data"].get("items", [])
            has_more = data["data"].get("has_more", False)
            page_token = data["data"].get("page_token")
            scanned += len(items)
            for it in items:
                # list 接口返回的 fields 字典 key 是字段名
                fv = it.get("fields", {}).get(self.tweet_id_field)
                if isinstance(fv, str) and fv == target:
                    return True
                if isinstance(fv, list):
                    for cell in fv:
                        if isinstance(cell, dict) and str(cell.get("text", "")) == target:
                            return True
            if not has_more or not page_token:
                break
            if scanned >= 5000:
                break
        return False

    # ---------------- 写记录 ----------------
    @staticmethod
    def _to_ms(value) -> int | None:
        """把 ISO 时间或 datetime 转成 ms 时间戳（Bitable DateTime 字段要求 ms）。"""
        if value is None:
            return None
        if isinstance(value, (int, float)):
            # 假定为秒或 ms；ms 一定 >= 10^12
            return int(value) if value >= 10**12 else int(value * 1000)
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return int(value.timestamp() * 1000)
        if isinstance(value, str):
            v = value.strip()
            if not v:
                return None
            try:
                # ISO8601（带 Z 或 +00:00）
                if v.endswith("Z"):
                    v = v[:-1] + "+00:00"
                dt = datetime.fromisoformat(v)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return int(dt.timestamp() * 1000)
            except Exception:
                pass
            # 试 yyyy-MM-dd HH:mm:ss
            try:
                dt = datetime.strptime(v, "%Y-%m-%d %H:%M:%S")
                dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
                return int(dt.timestamp() * 1000)
            except Exception:
                return None
        return None

    def add_record(self, fields: dict) -> dict:
        """写入一条记录。

        :param fields: {字段名: 值}，值按字段类型适配：
            - Text: str
            - URL: str 或 {"link": "...", "text": "..."}
            - DateTime: int (ms) 或 datetime 或 ISO 字符串
        """
        # Bitable add_record 接受字段名（不是 field_id）
        field_map = self.get_field_id_map()
        body_fields: dict = {}
        for name, raw in fields.items():
            if name not in field_map:
                # 跳过未知字段（不阻断写入）
                continue
            # URL 字段特殊处理
            if name == "推文链接" and isinstance(raw, str):
                body_fields[name] = {"link": raw, "text": raw}
                continue
            # DateTime 字段转 ms
            if name in ("发帖时间", "采集时间"):
                ms = self._to_ms(raw)
                if ms is not None:
                    body_fields[name] = ms
                continue
            body_fields[name] = raw

        body = {"fields": body_fields, "automatic_fields": False}
        url = f"{self.base}/bitable/v1/apps/{self.app_token}/tables/{self.table_id}/records"
        r = self._request_with_token_retry("POST", url, json_body=body)
        data = r.json()
        if data.get("code") != 0:
            raise RuntimeError(f"写入记录失败: {json.dumps(data, ensure_ascii=False)[:300]}")
        return data["data"].get("record", {})


def build_record_payload(tweet_data: dict, account: str | None = None, now: datetime | None = None) -> dict:
    """把抓到的 tweet_data 转换成 Bitable 字段值字典。

    tweet_data 通常形如：
        {"id": "...", "author": "...", "created_at": "ISO", "original_text": "...",
         "tweet_url": "...", "timestamp": "ISO", "translation": "..."}
    """
    now = now or datetime.now()
    author = tweet_data.get("author") or account or "Unknown"
    created_at = tweet_data.get("created_at") or tweet_data.get("createdAt")
    original_text = tweet_data.get("original_text") or tweet_data.get("text", "")
    translation = tweet_data.get("translation", "")
    tweet_url = tweet_data.get("tweet_url", "")

    # 文本字段：用第一行做"标题"，全文做"原文"，翻译做"译文"
    first_line = original_text.splitlines()[0].strip() if original_text else ""
    title = first_line[:60] + ("…" if len(first_line) > 60 else "") if first_line else "（无文本）"

    payload = {
        "推文ID": str(tweet_data.get("id", "")),
        "文本": title,
        "原文": original_text,
        "译文": translation,
        "作者": author,
        "推文链接": tweet_url,
        "发帖时间": created_at,
        "采集时间": now,
    }
    return payload
