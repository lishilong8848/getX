import requests
import time
import json
import os
import base64
import html
import re
from datetime import datetime, timedelta, timezone

from feishu_bitable import FeishuBitable, build_record_payload as build_bitable_payload


NITTER_SOURCES = (
    "https://xcancel.com",
    "https://nitter.net",
    "https://nitter.tiekoetter.com",
    "https://nitter.space",
)

BAD_TRANSLATION_MARKERS = ("AI处理失败", "翻译异常", "处理异常", "Invalid URL", "内容安全检查失败")
_ARGOS_READY = False


def parse_nitter_time(value: str) -> datetime | None:
    match = re.search(r"([A-Z][a-z]{2}) (\d{1,2}), (\d{4}) · (\d{1,2}):(\d{2}) (AM|PM) UTC", value)
    if not match:
        return None
    month_names = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
    month, day, year, hour, minute, meridiem = match.groups()
    hour = int(hour) % 12 + (12 if meridiem == "PM" else 0)
    return datetime(int(year), month_names.index(month) + 1, int(day), hour, int(minute))


def clean_html(value: str) -> str:
    value = re.sub(r"<br\s*/?>", "\n", value)
    return html.unescape(re.sub(r"<[^>]+>", "", value)).strip()


def parse_nitter_tweets(page_html: str, account: str, since_time: datetime, until_time: datetime, exclude_replies: bool = False) -> list:
    tweets = []
    blocks = re.findall(r'<div class="timeline-item[^"]*"[^>]*data-username="([^"]+)"[^>]*>(.*?)(?=<div class="timeline-item|\Z)', page_html, re.S)
    for author, block in blocks:
        if author.lower() != account.lower():
            continue
        link = re.search(r'<a class="tweet-link" href="([^"]*/status/(\d+)[^"]*)"', block)
        content = re.search(r'<div class="tweet-content[^"]*"[^>]*>(.*?)</div>\s*<div class="tweet-stats"', block, re.S)
        date = re.search(r'<span class="tweet-date"><a [^>]*title="([^"]+)"', block)
        if not link or not content or not date:
            continue
        text = clean_html(content.group(1))
        created_at = parse_nitter_time(html.unescape(date.group(1)))
        if not text or not created_at or (exclude_replies and text.startswith("@")):
            continue
        if since_time <= created_at <= until_time:
            tweets.append({"id": link.group(2), "text": text, "createdAt": created_at.strftime("%Y-%m-%dT%H:%M:%SZ"), "author": author or account})
    return tweets


def _extract_tweet_text_from_individual_page(page_html: str) -> str | None:
    """从单条推文页 (x.com/{user}/status/{id}) 提取完整正文。

    单条推文页的 SSR HTML 里：
    - og:description 通常截断到 300 字符（不够）
    - 渲染后的 <h1 class="sr-only"> 包含全文（屏幕阅读器用的隐藏标题）
    这里优先用 <h1 class="sr-only">（最完整），找不到再回退到 og:description。
    """
    # 1) <h1 class="sr-only"> 节点（屏幕阅读器标题，含全文）
    m = re.search(r'<h1[^>]*class="sr-only"[^>]*>(.*?)</h1>', page_html, re.S)
    if m:
        raw = html.unescape(m.group(1)).strip()
        # 形如 `Tibo on X: "..."` — 去掉作者前缀和包裹引号
        m2 = re.search(r'^.*?X:\s*["\u201c](.+)["\u201d]\s*$', raw, re.S)
        if m2:
            text = m2.group(1).strip()
        else:
            # 兜底：去掉 `XX on X: ` 前缀
            idx = raw.find('X: ')
            text = raw[idx+3:].strip().strip('"').strip() if idx >= 0 else raw
        if text:
            return text
    # 2) data-testid="tweetText"
    m = re.search(r'<div[^>]*data-testid="tweetText"[^>]*>(.*?)</div>\s*<div[^>]*data-testid=', page_html, re.S)
    if m:
        text = clean_html(m.group(1)).strip()
        if text:
            return text
    # 3) og:description（保底，截断到 300 字符）
    m = re.search(r'<meta property="og:description" content="([^"]*)"', page_html)
    if m:
        return html.unescape(m.group(1)).strip()
    return None


def fetch_full_texts(tweet_ids: list, headers: dict | None = None, timeout: int = 15) -> dict:
    """对一组推文 id 单独请求 /status/{id} 页面以获取完整正文。
    返回 {tweet_id: full_text} 字典，请求失败的 id 不在结果里。
    """
    if not tweet_ids:
        return {}
    headers = headers or {"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US,en;q=0.9"}
    out: dict = {}
    for tid in tweet_ids:
        try:
            r = requests.get(f"https://x.com/i/status/{tid}", headers=headers, timeout=timeout)
            if r.status_code != 200:
                continue
            full = _extract_tweet_text_from_individual_page(r.text)
            if full:
                out[tid] = full
        except Exception as error:
            print(f"⚠️ 获取完整推文 {tid} 失败: {error}")
    return out


def parse_x_profile_tweets(page_html: str, account: str, since_time: datetime, until_time: datetime, exclude_replies: bool = False) -> tuple[list, list]:
    """解析 X.com 个人主页。

    返回 (tweets, long_tweet_ids)：
    - tweets: 解析得到的推文列表（长推文使用截断版 full_text，待后续 fetch_full_texts 补全）
    - long_tweet_ids: 带有 note_tweet 引用、需要单独请求 /status/{id} 页面以拿到完整正文的长推文 id
    """
    tweets = []
    long_tweet_ids = []
    seen = set()
    tweet_ids = re.findall(rf'data-href="/{re.escape(account)}/status/(\d+)"', page_html)
    for tweet_id in tweet_ids:
        if tweet_id in seen:
            continue

        encoded_id = base64.b64encode(f"Tweet:{tweet_id}".encode()).decode()
        match = re.search(rf'"client:{re.escape(encoded_id)}:details".*?full_text:"((?:\\.|[^"\\])*)".*?created_at_ms:(\d+)', page_html, re.S)
        if not match:
            continue
        raw_text, created_ms = match.groups()
        seen.add(tweet_id)
        try:
            text = html.unescape(json.loads(f'"{raw_text}"')).strip()
            created_at = datetime.fromtimestamp(int(created_ms) / 1000, timezone.utc).replace(tzinfo=None)
        except Exception:
            continue
        if not text or (exclude_replies and text.startswith("@")):
            continue
        if not (since_time <= created_at <= until_time):
            continue
        # 检测该 Tweet 记录是否带 note_tweet 引用（长推文标记）
        if re.search(rf'note_tweet:\$R\[\d+\]=\{{__ref:"client:{re.escape(encoded_id)}:note_tweet"\}}', page_html):
            long_tweet_ids.append(tweet_id)
        tweets.append({"id": tweet_id, "text": text, "createdAt": created_at.strftime("%Y-%m-%dT%H:%M:%SZ"), "author": account})
    tweets.sort(key=lambda item: item["createdAt"], reverse=True)
    return tweets, long_tweet_ids


def format_tweet_post_time(created_at: str) -> str:
    if not created_at:
        return "未知时间"
    try:
        if "T" in created_at:
            utc_time = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        else:
            from email.utils import parsedate_to_datetime
            utc_time = parsedate_to_datetime(created_at)
        if utc_time.tzinfo is None:
            utc_time = utc_time.replace(tzinfo=timezone.utc)
        return utc_time.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    except Exception as error:
        print(f"❌ 通知时间解析错误: {error}")
        return "未知时间"


def should_translate_to_chinese(text: str) -> bool:
    text = re.sub(r"https?://\S+", "", text or "").strip()
    # ponytail: ASCII/CJK heuristic; add a real language detector only if mixed-language tweets matter.
    return bool(re.search(r"[A-Za-z]{3,}", text)) and not re.search(r"[\u4e00-\u9fff]", text)


def get_argos_translation(text: str) -> str:
    global _ARGOS_READY
    import argostranslate.package
    import argostranslate.translate

    if not _ARGOS_READY:
        try:
            argostranslate.translate.translate("Hello", "en", "zh")
        except Exception:
            argostranslate.package.update_package_index()
            package = next(
                (item for item in argostranslate.package.get_available_packages() if item.from_code == "en" and item.to_code == "zh"),
                None,
            )
            if package is None:
                raise RuntimeError("未找到 Argos en->zh 模型")
            argostranslate.package.install_from_path(package.download())
        _ARGOS_READY = True

    return argostranslate.translate.translate(text, "en", "zh").strip()


class TwitterAIMonitor:
    """Twitter 推文监控和本地翻译转发器"""
    
    def __init__(self, twitter_api_key: str, llm_url: str = "", llm_api_key: str = "", data_dir: str = "data",
                 dingtalk_webhook: str = "", dingtalk_secret: str = "", enable_dingtalk: bool = False,
                 ai_max_retries: int = 3, ai_timeout: int = 30, ai_max_tokens: int = 1000,
                 deepl_api_key: str = "", push_channel: str = "none", feishu_webhook: str = "",
                 feishu_secret: str = "",
                 feishu_app_id: str = "", feishu_app_secret: str = "",
                 feishu_app_token: str = "", feishu_table_id: str = ""):
        """
        初始化监控器

        :param twitter_api_key: 兼容旧配置，公开抓取不再使用
        :param llm_url: 大模型接口URL
        :param llm_api_key: 兼容旧配置，已不再使用
        :param data_dir: 数据存储目录（仅兼容旧调用，存储已迁到飞书多维表格）
        :param dingtalk_webhook: 钉钉机器人Webhook地址
        :param dingtalk_secret: 钉钉机器人签名密钥
        :param enable_dingtalk: 是否启用钉钉推送
        :param ai_max_retries: AI调用最大重试次数
        :param ai_timeout: AI调用超时时间（秒）
        :param ai_max_tokens: AI调用最大token数量
        :param feishu_app_id / feishu_app_secret / feishu_app_token / feishu_table_id: 飞书多维表格凭据
        """
        self.twitter_api_key = twitter_api_key
        self.data_dir = data_dir
        self.dingtalk_webhook = dingtalk_webhook
        self.dingtalk_secret = dingtalk_secret
        self.enable_dingtalk = enable_dingtalk
        self.push_channel = push_channel
        self.feishu_webhook = feishu_webhook
        self.feishu_secret = feishu_secret
        self.ai_max_retries = ai_max_retries
        self.ai_timeout = ai_timeout
        self.ai_max_tokens = ai_max_tokens
        # 飞书多维表格客户端（用于查重 + 持久化）
        if feishu_app_id and feishu_app_secret and feishu_app_token and feishu_table_id:
            self.bitable: FeishuBitable | None = FeishuBitable(
                app_id=feishu_app_id,
                app_secret=feishu_app_secret,
                app_token=feishu_app_token,
                table_id=feishu_table_id,
            )
        else:
            self.bitable = None
        # 兼容旧调用：data_dir 不再用于写入，仅在 bitable 不可用时作为兜底读取历史 JSON
        os.makedirs(data_dir, exist_ok=True)

    def translate(self, text: str) -> str:
        if not should_translate_to_chinese(text):
            return ""
        try:
            translated = get_argos_translation(text)
            return "" if translated == text.strip() else translated
        except Exception as error:
            print(f"❌ 本地翻译失败，使用原文: {error}")
            return ""

    def ensure_translation(self, tweet_data: dict) -> dict:
        if any(marker in tweet_data.get('translation', '') for marker in BAD_TRANSLATION_MARKERS):
            tweet_data.pop('translation', None)
        if not tweet_data.get('translation'):
            translation = self.translate(tweet_data.get('original_text') or tweet_data.get('text', ''))
            if translation:
                tweet_data['translation'] = translation
        return tweet_data

    def backfill_translations(self) -> int:
        translated_count = 0
        if not os.path.exists(self.data_dir):
            return 0
        for filename in os.listdir(self.data_dir):
            if not filename.startswith("tweets_") or not filename.endswith(".json"):
                continue
            file_path = os.path.join(self.data_dir, filename)
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    tweets = json.load(f)
            except json.JSONDecodeError:
                continue

            changed = False
            for tweet in tweets:
                if tweet.get('original_text', '').startswith('@'):
                    continue
                old_translation = tweet.get('translation')
                self.ensure_translation(tweet)
                if tweet.get('translation') != old_translation:
                    changed = True
                    if tweet.get('translation'):
                        translated_count += 1

            if changed:
                with open(file_path, 'w', encoding='utf-8') as f:
                    json.dump(tweets, f, ensure_ascii=False, indent=2)
        return translated_count

    def get_tweets_from_account(self, account: str, since_time: datetime, until_time: datetime, exclude_replies: bool = False) -> list:
        """从公开页面免费抓取最新推文。"""
        errors = []
        try:
            response = requests.get(
                f"https://x.com/{account}?lang=en",
                timeout=20,
                headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US,en;q=0.9"},
            )
            response.raise_for_status()
            tweets, long_ids = parse_x_profile_tweets(response.text, account, since_time, until_time, exclude_replies)
            if tweets or "full_text" in response.text:
                # 对带 note_tweet 引用的长推文，单独请求 /status/{id} 拿完整正文
                if long_ids:
                    full_texts = fetch_full_texts(long_ids)
                    for t in tweets:
                        if t["id"] in full_texts:
                            t["text"] = full_texts[t["id"]]
                return tweets
            errors.append("https://x.com: 页面无推文")
        except Exception as error:
            errors.append(f"https://x.com: {error}")

        for source in NITTER_SOURCES:
            try:
                response = requests.get(f"{source}/{account}", timeout=15, headers={"User-Agent": "Mozilla/5.0"})
                response.raise_for_status()
                tweets = parse_nitter_tweets(response.text, account, since_time, until_time, exclude_replies)
                if tweets or "timeline-item" in response.text:
                    return tweets
                errors.append(f"{source}: 页面无推文")
            except Exception as error:
                errors.append(f"{source}: {error}")
        raise RuntimeError("公开抓取失败：" + "；".join(errors))
    
    def save_tweet_data(self, tweet_data: dict):
        """
        持久化推文：查重 + 写入飞书多维表格。

        - 查重：在多维表中按 tweet_id 判断；不在本地 JSON 判重。
        - 写入：把字段映射到多维表格（推文ID/文本/原文/译文/作者/推文链接/发帖时间/采集时间）。
        - 如果未配置飞书凭据，回退到旧本地 JSON 行为（兼容单元测试等场景）。
        """
        tweet_id = tweet_data.get('id')
        author = tweet_data.get('author', 'Unknown')

        # 1) 多维表格查重
        if self.bitable and tweet_id:
            try:
                if self.bitable.record_exists(str(tweet_id)):
                    print(f"⏭️ 跳过重复推文(多维表格已存在): {tweet_id} - {author}")
                    return
            except Exception as err:
                print(f"⚠️ 多维表格查重失败，降级为本地写入: {err}")

        # 2) 本地翻译（如未翻译）
        self.ensure_translation(tweet_data)

        # 3) 写入多维表格
        if self.bitable:
            try:
                payload = build_bitable_payload(tweet_data)
                rec = self.bitable.add_record(payload)
                print(f"✅ 写入飞书多维表格: {tweet_id} - {author} (record_id={rec.get('record_id')})")
                self.send_push_notification(tweet_data)
                return
            except Exception as err:
                print(f"❌ 飞书多维表格写入失败: {err}")

        # 4) 兜底：写本地 JSON（仅在未配置多维表格时使用）
        today = datetime.now().strftime("%Y-%m-%d")
        file_path = os.path.join(self.data_dir, f"tweets_{today}.json")
        existing_data = []
        if os.path.exists(file_path):
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    existing_data = json.load(f)
            except json.JSONDecodeError:
                existing_data = []
        existing_ids = {item.get('id') for item in existing_data if item.get('id')}
        if tweet_id not in existing_ids:
            existing_data.append(tweet_data)
            print(f"保存新推文(本地): {tweet_id} - {author}")
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(existing_data, f, ensure_ascii=False, indent=2)
            self.send_push_notification(tweet_data)
        else:
            print(f"跳过重复推文(本地): {tweet_id} - {author}")

    def send_push_notification(self, tweet_data: dict) -> bool:
        if self.push_channel == "dingtalk" and self.dingtalk_webhook and self.dingtalk_secret:
            return self.send_dingtalk_notification(tweet_data)
        if self.push_channel == "feishu" and self.feishu_webhook and self.feishu_secret:
            return self.send_feishu_notification(tweet_data)
        return False
    
    def send_dingtalk_notification(self, tweet_data: dict) -> bool:
        """
        发送钉钉机器人通知
        
        :param tweet_data: 推文数据
        """
        try:
            import hmac
            import hashlib
            import base64
            import urllib.parse
            import requests
            
            # 构建消息内容
            author = tweet_data.get('author', 'Unknown')
            original_created_at_str = tweet_data.get('created_at', '') # 获取原始推文创建时间字符串
            original_text = tweet_data.get('original_text', '')
            translation = tweet_data.get('translation', '')
            tweet_url = tweet_data.get('tweet_url', '')
            formatted_tweet_posting_time = format_tweet_post_time(original_created_at_str)
            translation_block = f"\n## 🇨🇳 **中文翻译：**\n{translation}\n" if translation else ""
            
            message = f"""# 📨 X 动态推送

---

## 📝 **作者：** {author}
⏰ **发帖时间：** {formatted_tweet_posting_time}

## 📝 **推文原文：**
{original_text}
{translation_block}

{f"🔗 [查看原推文]({tweet_url})" if tweet_url else ""}

---

💡 *由 Twitter(X) 监控系统自动推送*"""
            
            # 计算签名
            timestamp = str(int(time.time() * 1000))
            string_to_sign = f'{timestamp}\n{self.dingtalk_secret}'
            hmac_code = hmac.new(
                self.dingtalk_secret.encode('utf-8'),
                string_to_sign.encode('utf-8'),
                digestmod=hashlib.sha256
            ).digest()
            sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
            
            # 构建请求URL
            url = f"{self.dingtalk_webhook}&timestamp={timestamp}&sign={sign}"
            
            # 构建消息数据
            data = {
                "msgtype": "markdown",
                "markdown": {
                    "title": f"📨 X 动态 - {author}",
                    "text": message
                },
                "at": {
                    "isAtAll": False
                }
            }
            
            # 发送请求
            response = requests.post(url, json=data, timeout=10)
            
            if response.status_code == 200:
                result = response.json()
                if result.get('errcode') == 0:
                    print(f"✅ 钉钉消息发送成功: {author}")
                    return True
                else:
                    print(f"❌ 钉钉消息发送失败: {result.get('errmsg')}")
                    return False
            else:
                print(f"❌ 钉钉请求失败: HTTP {response.status_code}")
                return False
                
        except Exception as e:
            print(f"❌ 钉钉消息发送异常: {str(e)}")
            return False

    def send_feishu_notification(self, tweet_data: dict) -> bool:
        """使用签名校验发送飞书机器人消息。"""
        try:
            import base64
            import hashlib
            import hmac

            timestamp = str(int(time.time()))
            sign = base64.b64encode(hmac.new(f"{timestamp}\n{self.feishu_secret}".encode(), digestmod=hashlib.sha256).digest()).decode()
            author = tweet_data.get("author", "Unknown")
            original = tweet_data.get("original_text", "")
            translation = tweet_data.get("translation", "")
            post_time = format_tweet_post_time(tweet_data.get("created_at", ""))
            tweet_url = tweet_data.get("tweet_url", "")
            translation_text = f"\n\n中文：{translation}" if translation else ""
            response = requests.post(self.feishu_webhook, json={
                "timestamp": timestamp,
                "sign": sign,
                "msg_type": "text",
                "content": {"text": f"📨 X 动态 - @{author}\n\n发帖时间：{post_time}\n\n原文：{original}{translation_text}" + (f"\n\n链接：{tweet_url}" if tweet_url else "")},
            }, timeout=10)
            result = response.json()
            success = response.ok and result.get("code", result.get("StatusCode", 0)) == 0
            print(f"{'✅' if success else '❌'} 飞书消息发送{' 成功' if success else '失败'}: {author}")
            return success
        except Exception as error:
            print(f"❌ 飞书消息发送异常: {error}")
            return False
    
    def load_tweets_by_date(self, date_str: str = None) -> list:
        """
        根据日期加载推文数据（已迁移到飞书多维表格）。

        :param date_str: 日期字符串 (YYYY-MM-DD)，默认为今天
        :return: 推文数据列表
        """
        if date_str is None:
            date_str = datetime.now().strftime("%Y-%m-%d")
        all_tweets = self.get_all_tweets()
        return [t for t in all_tweets if (t.get("processed_date") or "") == date_str or
                                       str(t.get("created_at", ""))[:10] == date_str]

    def _bitable_field_value(self, fv) -> str:
        """从 Bitable 字段值里抽取展示用字符串（处理 cell 列表/单值/字典）。"""
        if fv is None:
            return ""
        if isinstance(fv, str):
            return fv
        if isinstance(fv, (int, float)):
            return str(fv)
        if isinstance(fv, list):
            parts = []
            for c in fv:
                if isinstance(c, dict):
                    txt = c.get("text") or c.get("name") or c.get("link")
                    if txt:
                        parts.append(str(txt))
                else:
                    parts.append(str(c))
            return "\n".join(parts)
        if isinstance(fv, dict):
            return fv.get("text") or fv.get("link") or json.dumps(fv, ensure_ascii=False)
        return str(fv)

    @staticmethod
    def _ms_to_iso(ms) -> str:
        if not ms:
            return ""
        try:
            ms = int(ms)
            return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%dT%H:%M:%S+08:00")
        except Exception:
            return ""

    def _record_to_tweet_dict(self, rec_fields: dict, field_name_map: dict) -> dict:
        """把多维表格返回的 fields 字典转回前端用的 tweet dict（兼容旧字段名）。"""
        # 反向映射：把 field_id → field_name
        id_to_name = {v: k for k, v in field_name_map.items()}
        normalized = {id_to_name.get(k, k): v for k, v in rec_fields.items()}

        tweet_id = self._bitable_field_value(normalized.get("推文ID"))
        original_text = self._bitable_field_value(normalized.get("原文"))
        translation = self._bitable_field_value(normalized.get("译文"))
        title = self._bitable_field_value(normalized.get("文本"))
        author = self._bitable_field_value(normalized.get("作者"))
        link = self._bitable_field_value(normalized.get("推文链接"))
        created_ms = normalized.get("发帖时间")
        collected_ms = normalized.get("采集时间")
        created_at = self._ms_to_iso(created_ms)
        timestamp = self._ms_to_iso(collected_ms)

        return {
            "id": tweet_id,
            "author": author,
            "original_text": original_text,
            "text": original_text,
            "translation": translation,
            "title": title,
            "tweet_url": link,
            "created_at": created_at,
            "createdAt": created_at,
            "timestamp": timestamp,
            "processed_date": datetime.fromtimestamp(int(collected_ms) / 1000, tz=timezone(timedelta(hours=8))).strftime("%Y-%m-%d") if collected_ms else "",
            "source": "feishu_bitable",
        }

    def _read_from_bitable(self) -> list:
        """从飞书多维表格拉取全部记录，转为前端可用的 tweet dict 列表。"""
        if not self.bitable:
            return []
        try:
            field_map = self.bitable.get_field_id_map()
            url = f"{self.bitable.base}/bitable/v1/apps/{self.bitable.app_token}/tables/{self.bitable.table_id}/records"
            items: list = []
            page_token = None
            while True:
                params = {"page_size": 500, "automatic_fields": "false"}
                if page_token:
                    params["page_token"] = page_token
                r = requests.get(url, params=params, headers=self.bitable._headers(), timeout=self.bitable.timeout)
                data = r.json()
                if data.get("code") != 0:
                    print(f"⚠️ 拉取多维表格记录失败: {data.get('msg')}")
                    return items
                items.extend(data["data"].get("items", []))
                if not data["data"].get("has_more") or not data["data"].get("page_token"):
                    break
                page_token = data["data"].get("page_token")
            return [self._record_to_tweet_dict(it.get("fields", {}), field_map) for it in items]
        except Exception as err:
            print(f"⚠️ 读取飞书多维表格失败: {err}")
            return []

    def _read_from_local_legacy(self) -> list:
        """兜底：从旧本地 JSON 读取历史数据（仅用于无多维表格时的兼容）。"""
        all_tweets = []
        if not os.path.exists(self.data_dir):
            return all_tweets
        for filename in os.listdir(self.data_dir):
            if filename.startswith("tweets_") and filename.endswith(".json"):
                file_path = os.path.join(self.data_dir, filename)
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        tweets = json.load(f)
                        for tweet in tweets:
                            if any(marker in tweet.get('translation', '') for marker in BAD_TRANSLATION_MARKERS):
                                tweet.pop('translation', None)
                            tweet.pop('ai_title', None)
                            tweet.pop('ai_translation', None)
                            tweet.pop('ai_analysis', None)
                        all_tweets.extend(tweets)
                except json.JSONDecodeError:
                    continue
        all_tweets.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
        return all_tweets

    def get_all_tweets(self) -> list:
        """
        获取所有存储的推文数据。优先从飞书多维表格读取（如果已配置），
        否则从本地 JSON 文件兜底读取（兼容历史数据）。
        """
        if self.bitable:
            all_tweets = self._read_from_bitable()
        else:
            all_tweets = self._read_from_local_legacy()
        # 按时间倒序
        all_tweets.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
        return all_tweets
    
    def monitor_and_process(self, target_accounts: list, check_interval: int = 300, hours: int = 1, exclude_replies: bool = False):
        """
        监控Twitter账号并使用AI处理新推文
        
        :param target_accounts: 要监控的账号列表
        :param check_interval: 检查间隔（秒）
        :param hours: 初始回溯时间（小时）
        :param exclude_replies: 是否排除回复推文
        """
        last_checked_time = datetime.utcnow() - timedelta(hours=hours)
        
        def check_and_process_tweets():
            nonlocal last_checked_time
            until_time = datetime.utcnow()
            since_time = last_checked_time
            
            all_tweets = []
            all_accounts_ok = True
            
            for account in target_accounts:
                try:
                    tweets = self.get_tweets_from_account(account, since_time, until_time, exclude_replies)
                    all_tweets.extend(tweets)
                except Exception as e:
                    all_accounts_ok = False
                    print(f"❌ 获取 @{account} 推文失败，保留检查窗口下轮重试: {str(e)}")
                
                # 添加5秒延迟，避免API限制
                if account != target_accounts[-1]:  # 如果不是最后一个账号，添加延迟
                    print("等待5秒，避免API请求限制...")
                    time.sleep(5)
            
            if all_tweets:
                print(f"发现 {len(all_tweets)} 条新推文，开始转发...\n")
                
                for idx, tweet in enumerate(all_tweets, start=1):
                    print(f"{'='*60}")
                    print(f"处理推文 {idx}/{len(all_tweets)}")
                    print(f"{'='*60}")
                    
                    # 基本信息
                    tweet_id = tweet.get('id') or tweet.get('id_str')
                    tweet_url = f"https://twitter.com/{tweet['author']}/status/{tweet_id}"
                    original_text = tweet.get('text', '')
                    
                    print(f"作者：{tweet['author']}")
                    print(f"发布时间：{tweet.get('createdAt')}")
                    print(f"原文：{original_text}")
                    print(f"链接：{tweet_url}")
                    print()
                    
                    print(f"{'='*60}\n")
                    
                    # 保存数据到JSON
                    tweet_data = {
                        'id': tweet_id,
                        'author': tweet['author'],
                        'created_at': tweet.get('createdAt'),
                        'original_text': original_text,
                        'tweet_url': tweet_url,
                        'timestamp': datetime.utcnow().isoformat(),
                        'processed_date': datetime.now().strftime("%Y-%m-%d")
                    }
                    self.save_tweet_data(tweet_data)
                    
                    # 添加延迟避免API频率限制
                    time.sleep(2)
            elif not all_accounts_ok:
                print(f"{datetime.utcnow()} - 抓取失败，保留上次检查时间，下轮继续补抓。")
            else:
                print(f"{datetime.utcnow()} - 没有发现新推文。")
            
            if all_accounts_ok:
                last_checked_time = until_time
        
        print(f"开始监控账号: {', '.join(target_accounts)}")
        print(f"检查间隔: {check_interval} 秒")
        print("本地翻译转发已启用\n")
        
        try:
            while True:
                check_and_process_tweets()
                print(f"等待 {check_interval} 秒后进行下次检查...")
                time.sleep(check_interval)
        except KeyboardInterrupt:
            print("监控已停止。")
    
    def monitor_and_process_with_status(self, target_accounts: list, check_interval: int = 300, hours: int = 1, status_dict: dict = None, exclude_replies: bool = False):
        """
        带状态更新的监控功能
        
        :param target_accounts: 要监控的账号列表
        :param check_interval: 检查间隔（秒）
        :param hours: 初始回溯时间（小时）
        :param status_dict: 状态字典，用于更新前端显示
        :param exclude_replies: 是否排除回复推文
        """
        last_checked_time = datetime.utcnow() - timedelta(hours=hours)
        
        def update_status(status, account="", result=""):
            if status_dict:
                status_dict["current_status"] = status
                status_dict["current_account"] = account
                status_dict["last_update"] = datetime.now().isoformat()
                if result:
                    status_dict["last_result"] = result
                # 计算下次检查时间
                next_time = datetime.now() + timedelta(seconds=check_interval)
                status_dict["next_check_time"] = next_time.isoformat()
        
        def check_and_process_tweets():
            nonlocal last_checked_time
            until_time = datetime.utcnow()
            since_time = last_checked_time
            
            all_tweets = []
            all_accounts_ok = True
            
            try:
                # 更新状态：开始抓取
                update_status("🔍 扫描中", f"{', '.join(target_accounts)}")
                
                for account in target_accounts:
                    try:
                        update_status(f"📡 正在抓取 @{account} 的推文...")
                        tweets = self.get_tweets_from_account(account, since_time, until_time, exclude_replies)
                        all_tweets.extend(tweets)
                        print(f"✅ 成功获取 @{account} 的 {len(tweets)} 条推文")
                        
                        # 添加5秒延迟，避免API限制
                        if account != target_accounts[-1]:  # 如果不是最后一个账号，添加延迟
                            print("等待5秒，避免API请求限制...")
                            time.sleep(5)
                            
                    except Exception as e:
                        all_accounts_ok = False
                        print(f"❌ 获取 @{account} 推文失败: {str(e)}")
                        update_status(f"⚠️ @{account} 数据获取异常", result=f"错误: {str(e)}")
                        continue
            except Exception as e:
                print(f"❌ 推文扫描过程出错: {str(e)}")
                update_status(f"⚠️ 扫描过程异常", result=f"错误: {str(e)}")
                return
            
            if all_tweets:
                update_status(f"📨 发现 {len(all_tweets)} 条新推文，转发中...", result=f"找到 {len(all_tweets)} 条新推文")
                
                for idx, tweet in enumerate(all_tweets, start=1):
                    # 基本信息
                    tweet_id = tweet.get('id') or tweet.get('id_str')
                    tweet_url = f"https://twitter.com/{tweet['author']}/status/{tweet_id}"
                    original_text = tweet.get('text', '')
                    
                    update_status(f"📨 转发中... ({idx}/{len(all_tweets)})", f"@{tweet['author']}")
                    
                    # 保存数据到JSON
                    tweet_data = {
                        'id': tweet_id,
                        'author': tweet['author'],
                        'created_at': tweet.get('createdAt'),
                        'original_text': original_text,
                        'tweet_url': tweet_url,
                        'timestamp': datetime.utcnow().isoformat(),
                        'processed_date': datetime.now().strftime("%Y-%m-%d")
                    }
                    self.save_tweet_data(tweet_data)
                    
                    # 更新处理计数
                    if status_dict:
                        status_dict["processed_tweets"] = status_dict.get("processed_tweets", 0) + 1
                    
                    # 添加延迟避免API频率限制
                    time.sleep(2)
                
                update_status("✅ 处理完成", result=f"成功处理 {len(all_tweets)} 条推文")
            elif not all_accounts_ok:
                update_status("⚠️ 抓取失败，下轮继续补抓", result="本轮抓取失败，未推进检查时间")
            else:
                update_status("⭐ 智能待机中", result="未发现新推文，继续监控中...")
            
            if all_accounts_ok:
                last_checked_time = until_time
        
        update_status("🚀 Neural Network 已启动", f"监控 {len(target_accounts)} 个账号")
        print(f"🚀 监控启动成功，目标账号: {target_accounts}")
        
        try:
            while status_dict and status_dict.get("running", False):
                print(f"🔄 开始新一轮检查循环...")
                check_and_process_tweets()
                
                # 倒计时等待
                for remaining in range(check_interval, 0, -10):
                    if not status_dict.get("running", False):
                        print("🛑 收到停止信号，退出监控")
                        break
                    update_status(f"⏱️ 下次扫描倒计时 {remaining}s", result=status_dict.get("last_result", ""))
                    time.sleep(10)
                    
        except KeyboardInterrupt:
            print("🛑 监控被中断")
            update_status("🛑 Neural Network 已停止")
        except Exception as e:
            print(f"❌ 监控过程出现异常: {str(e)}")
            update_status("❌ 监控异常停止", result=f"错误: {str(e)}")
            if status_dict:
                status_dict["running"] = False

# 主程序
if __name__ == "__main__":
    # 从配置文件加载配置
    config_file = "config.json"
    
    # 默认配置
    default_config = {
        "TWITTER_API_KEY": "b74c1eefe1004xxx3c6b82c4ee5",
        "LLM_URL": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "LLM_API_KEY": "sk-bf2a9bf3xxxb344d8bbe5fbdc",
        "TARGET_ACCOUNTS": ["OpenAI"],
        "CHECK_INTERVAL": 300,
        "INITIAL_HOURS": 64,
        "EXCLUDE_REPLIES": False, # 新增配置项
        "DINGTALK_WEBHOOK": "", # 新增配置项
        "DINGTALK_SECRET": "", # 新增配置项
        "ENABLE_DINGTALK": False # 新增配置项
    }
    
    # 读取配置文件
    if os.path.exists(config_file):
        try:
            with open(config_file, 'r', encoding='utf-8') as f:
                config = json.load(f)
                # 合并默认配置
                for key, value in default_config.items():
                    if key not in config:
                        config[key] = value
        except Exception as e:
            print(f"读取配置文件失败，使用默认配置: {e}")
            config = default_config
    else:
        print("配置文件不存在，使用默认配置")
        config = default_config
    
    # 提取配置参数
    TWITTER_API_KEY = config["TWITTER_API_KEY"]
    LLM_URL = config["LLM_URL"]
    LLM_API_KEY = config["LLM_API_KEY"]
    TARGET_ACCOUNTS = config["TARGET_ACCOUNTS"]
    CHECK_INTERVAL = config["CHECK_INTERVAL"]
    INITIAL_HOURS = config["INITIAL_HOURS"]
    EXCLUDE_REPLIES = config["EXCLUDE_REPLIES"] # 从配置加载
    DINGTALK_WEBHOOK = config["DINGTALK_WEBHOOK"]
    DINGTALK_SECRET = config["DINGTALK_SECRET"]
    ENABLE_DINGTALK = config["ENABLE_DINGTALK"]
    
    print(f"开始监控账号: {', '.join(TARGET_ACCOUNTS)}")
    print(f"检查间隔: {CHECK_INTERVAL}秒")
    print(f"初始回溯: {INITIAL_HOURS}小时")
    print(f"是否排除回复: {EXCLUDE_REPLIES}") # 打印配置
    print(f"是否启用钉钉推送: {ENABLE_DINGTALK}") # 打印配置
    
    # 创建监控器并开始监控
    monitor = TwitterAIMonitor(TWITTER_API_KEY, LLM_URL, LLM_API_KEY, 
                                dingtalk_webhook=DINGTALK_WEBHOOK, 
                                dingtalk_secret=DINGTALK_SECRET, 
                                enable_dingtalk=ENABLE_DINGTALK)
    monitor.monitor_and_process(TARGET_ACCOUNTS, CHECK_INTERVAL, INITIAL_HOURS, EXCLUDE_REPLIES)
