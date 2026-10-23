"""Poll Telegram public channels via t.me/s/ preview pages and forward updates to QQ groups.

The plugin fetches the public preview page of each configured channel
(https://t.me/s/<channel>, no API key or login required), keeps a per-channel
baseline of the last seen message id, and merges any newer messages into a
single forward message (合并转发) pushed to whitelisted QQ groups through the
aiocqhttp (OneBot v11) platform.
"""

import asyncio
import html
import json
import re
from pathlib import Path

import httpx
from bs4 import BeautifulSoup, Tag

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Image, Node, Nodes, Plain
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.pipeline.content_safety_check.strategies.strategy import (
    StrategySelector,
)

PLUGIN_NAME = "astrbot_plugin_tg2qq"
BASE_URL = "https://t.me/s/{channel}"
MAX_PAGES = (
    10  # max pages to walk back in one poll, so a long downtime won't lose messages
)
FETCH_ATTEMPTS = 3
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
STYLE_URL_RE = re.compile(r"background-image\s*:\s*url\(['\"]?(.*?)['\"]?\)")


def _html_to_text(node: Tag) -> str:
    """Convert a Telegram preview HTML node to plain text, keeping line breaks.

    Args:
        node: A BeautifulSoup node containing the message body.

    Returns:
        The plain text with collapsed whitespace and at most one blank line.
    """
    for br in node.find_all("br"):
        br.replace_with("\n")
    raw = node.get_text("")
    lines = [
        re.sub(r"[ \t\u00a0\u200b]+", " ", line).strip() for line in raw.split("\n")
    ]
    out: list[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip()


def _background_url(node: Tag) -> str | None:
    """Extract the image URL from an inline `background-image: url(...)` style.

    Args:
        node: The node whose style attribute holds the background image.

    Returns:
        The unescaped image URL, or None if the style has no background image.
    """
    match = STYLE_URL_RE.search(node.get("style", ""))
    if not match:
        return None
    url = html.unescape(match.group(1)).strip()
    return url or None


def parse_page(page_html: str) -> tuple[list[dict], str | None]:
    """Parse a t.me/s/ preview page into message dicts.

    Args:
        page_html: The raw HTML of the preview page.

    Returns:
        A tuple of (messages sorted by id, channel display title or None). Each
        message dict has: channel, id, date, text, links, images, video, reply.
    """
    soup = BeautifulSoup(page_html, "html.parser")
    title_node = soup.select_one('meta[property="og:title"]')
    title = title_node.get("content") if title_node else None

    messages: list[dict] = []
    for wrap in soup.select("div.tgme_widget_message_wrap"):
        div = wrap.select_one("div.tgme_widget_message")
        post = div.get("data-post") if div else None
        if not post or "/" not in post:
            continue
        channel_name, _, msg_id = post.rpartition("/")
        try:
            msg_id = int(msg_id)
        except ValueError:
            continue

        # A reply bubble embeds its own .tgme_widget_message_text holding the
        # quoted text, which comes first in document order; the real message
        # body is the first text node OUTSIDE the reply element.
        reply_node = wrap.select_one(".tgme_widget_message_reply")
        reply_text_node = (
            reply_node.select_one(".tgme_widget_message_text") if reply_node else None
        )
        text_node = None
        for node in wrap.select(".tgme_widget_message_text"):
            if reply_node is not None and reply_node in node.parents:
                continue
            text_node = node
            break
        text = _html_to_text(text_node) if text_node else ""
        # Posts that require opening in Telegram render as a placeholder notice;
        # treat them as contentless so the fallback (post link) kicks in.
        if "open Telegram to view this post" in text:
            text = ""

        # Keep external links that are not already visible in the text; skip
        # hashtag/mention links which Telegram renders as t.me URLs.
        links: list[str] = []
        for a in text_node.select("a[href]") if text_node else []:
            href = (a.get("href") or "").strip()
            visible = a.get_text(strip=True)
            if not href or href in text or href in links:
                continue
            if visible.startswith("#") or visible.startswith("@"):
                continue
            links.append(href)

        images: list[str] = []
        for photo in wrap.select(".tgme_widget_message_photo_wrap"):
            url = _background_url(photo)
            if url:
                images.append(url)
        sticker = wrap.select_one(".tgme_widget_message_sticker")
        if sticker:
            url = (
                _background_url(sticker)
                or sticker.get("src")
                or sticker.get("data-webp")
            )
            if url:
                images.append(url)

        has_video = bool(wrap.select_one("video, .tgme_widget_message_video_thumb"))

        reply = None
        if reply_node:
            author_node = reply_node.select_one(".tgme_widget_message_author")
            author = author_node.get_text(" ", strip=True) if author_node else ""
            reply_text = _html_to_text(reply_text_node) if reply_text_node else ""
            if author and reply_text:
                reply = f"{author}: {reply_text}"
            else:
                reply = (author or reply_text) or None

        date_node = wrap.select_one(".tgme_widget_message_date time")
        messages.append(
            {
                "channel": channel_name,
                "id": msg_id,
                "date": date_node.get("datetime") if date_node else None,
                "text": text,
                "links": links,
                "images": images,
                "video": has_video,
                "reply": reply,
            }
        )

    messages.sort(key=lambda m: m["id"])
    return messages, title


def normalize_channel(raw: str) -> str | None:
    """Normalize a user-supplied channel entry to its bare username.

    Args:
        raw: User input, e.g. "@foo", "foo", "t.me/foo" or "https://t.me/s/foo".

    Returns:
        The bare channel username, or None if nothing remains after cleanup.
    """
    name = re.sub(r"(?i)^(?:https?://)?(?:www\.)?t\.me/(?:s/)?", "", raw.strip())
    name = name.lstrip("@").split("?", 1)[0].split("/", 1)[0].strip()
    return name or None


class Tg2qqPlugin(Star):
    """Telegram public channel to QQ group forwarder."""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        self.client: httpx.AsyncClient | None = None
        self.task: asyncio.Task | None = None
        self.baseline: dict[str, int] = {}
        self.channels: list[str] = []
        self.whitelist: list[str] = []
        self.poll_interval = 30
        self.send_interval = 0.5
        self.skip_res: list[re.Pattern] = []
        self.filter_res: list[re.Pattern] = []
        self.exclude_res: list[re.Pattern] = []
        self.safety: StrategySelector | None = None

    async def initialize(self):
        """Read config, load the baseline and start the background polling task."""
        self.poll_interval = max(int(self.config.get("poll_interval_minutes") or 30), 1)
        self.send_interval = max(
            float(self.config.get("send_interval_seconds") or 0.5), 0.0
        )
        self.skip_res = self._compile_regexes("skip_push_regex")
        self.filter_res = self._compile_regexes("filter_regex")
        self.exclude_res = self._compile_regexes("exclude_regex")
        if self.config.get("use_content_safety"):
            try:
                safety_cfg = self.context.get_config().get("content_safety") or {}
                selector = StrategySelector(safety_cfg)
                if selector.enabled_strategies:
                    self.safety = selector
                    names = [type(s).__name__ for s in self.safety.enabled_strategies]
                    logger.info(f"[tg2qq] content safety strategies active: {names}")
                else:
                    logger.info(
                        "[tg2qq] content safety enabled but no strategy configured "
                        "in the main config, check stays off"
                    )
            except Exception:
                logger.exception(
                    "[tg2qq] failed to init content safety, check stays off"
                )
        self.whitelist = [
            str(g).strip()
            for g in (self.config.get("whitelist_groups") or [])
            if str(g).strip()
        ]
        for raw in self.config.get("tg_channels") or []:
            name = normalize_channel(str(raw))
            if name and name not in self.channels:
                self.channels.append(name)
        if not self.channels:
            logger.info("[tg2qq] no channel configured, plugin stays idle")
            return

        self.baseline = self._load_baseline()
        self.client = httpx.AsyncClient(
            headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
            timeout=30,
            follow_redirects=True,
        )
        self.task = asyncio.create_task(self._poll_loop())
        logger.info(
            f"[tg2qq] started, channels={self.channels}, "
            f"poll_interval={self.poll_interval}min, whitelist={self.whitelist or 'ALL groups'}"
        )

    async def terminate(self):
        """Cancel the polling task and release the HTTP client."""
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None
        if self.client:
            await self.client.aclose()
            self.client = None

    def _compile_regexes(self, key: str) -> list[re.Pattern]:
        """Compile a regex config entry into a list of patterns.

        The config value is normally a list where each item is one regex; a
        plain string (legacy single-regex config) is accepted as a one-item
        list. A message matches when ANY pattern matches.

        Args:
            key: Config key holding the pattern(s) (e.g. "skip_push_regex").

        Returns:
            Compiled case-insensitive patterns; invalid items are logged and
            dropped, an empty/missing value yields an empty list (disabled).
        """
        raw = self.config.get(key)
        if isinstance(raw, str):
            items = [raw]
        elif isinstance(raw, list):
            items = [str(item) for item in raw]
        else:
            items = []
        compiled: list[re.Pattern] = []
        for item in items:
            item = item.strip()
            if not item:
                continue
            try:
                compiled.append(re.compile(item, re.IGNORECASE))
            except re.error:
                logger.warning(
                    f"[tg2qq] invalid regex in '{key}', item disabled: {item!r}"
                )
        return compiled

    def _load_baseline(self) -> dict[str, int]:
        """Load the per-channel baseline (last seen message id) from disk.

        Returns:
            A dict mapping channel username to last seen message id; empty on
            missing or corrupted file.
        """
        path = self.data_dir / "baseline.json"
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("[tg2qq] failed to load baseline, starting fresh")
            return {}

    def _save_baseline(self) -> None:
        """Persist the baseline to disk; failures are logged, not raised."""
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            (self.data_dir / "baseline.json").write_text(
                json.dumps(self.baseline, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            logger.exception("[tg2qq] failed to save baseline")

    async def _poll_loop(self):
        """Poll all channels once immediately, then repeat every poll_interval."""
        while True:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[tg2qq] poll round failed")
            await asyncio.sleep(self.poll_interval * 60)

    async def _poll_once(self):
        """Poll every configured channel sequentially with a small pause between."""
        for channel in self.channels:
            try:
                await self._poll_channel(channel)
            except Exception:
                logger.exception(f"[tg2qq] poll channel {channel} failed")
            await asyncio.sleep(2)

    async def _fetch(self, channel: str, before: int | None) -> str | None:
        """Fetch one preview page with retries.

        Args:
            channel: The channel username.
            before: Optional message id; the page then contains older messages.

        Returns:
            The page HTML, or None if all attempts failed.
        """
        if self.client is None:
            return None
        url = BASE_URL.format(channel=channel)
        params = {"before": before} if before else None
        for attempt in range(FETCH_ATTEMPTS):
            try:
                resp = await self.client.get(url, params=params)
                resp.raise_for_status()
                return resp.text
            except httpx.HTTPError as e:
                logger.warning(f"[tg2qq] fetch {url} attempt {attempt + 1} failed: {e}")
                if attempt < FETCH_ATTEMPTS - 1:
                    await asyncio.sleep(2 * (attempt + 1))
        logger.error(f"[tg2qq] fetch {url} failed after {FETCH_ATTEMPTS} attempts")
        return None

    async def _poll_channel(self, channel: str):
        """Poll one channel and push messages newer than its baseline.

        Args:
            channel: The configured channel username (may redirect on t.me; the
                baseline is keyed by the real username parsed from data-post).
        """
        messages: list[dict] = []
        title: str | None = None
        real_channel: str | None = None
        before: int | None = None
        for _ in range(MAX_PAGES):
            page_html = await self._fetch(channel, before)
            if page_html is None:
                return  # fetch failed; keep the baseline untouched and retry next round
            page_msgs, title = parse_page(page_html)
            if not page_msgs:
                break
            if real_channel is None:
                real_channel = page_msgs[0]["channel"]
            known = {m["id"] for m in messages}
            messages.extend(m for m in page_msgs if m["id"] not in known)
            baseline = self.baseline.get(real_channel)
            # Stop paging once the page reaches the baseline; on the very first
            # fetch (no baseline) one page is enough to record it.
            if baseline is None or min(m["id"] for m in page_msgs) <= baseline:
                break
            before = min(m["id"] for m in page_msgs)
            await asyncio.sleep(1)

        if real_channel is None or not messages:
            logger.warning(
                f"[tg2qq] no messages parsed for {channel} (missing/private channel?)"
            )
            return

        baseline = self.baseline.get(real_channel)
        if baseline is None:
            self.baseline[real_channel] = max(m["id"] for m in messages)
            self._save_baseline()
            logger.info(
                f"[tg2qq] baseline set for @{real_channel}: {self.baseline[real_channel]}"
            )
            return

        new = sorted((m for m in messages if m["id"] > baseline), key=lambda m: m["id"])
        if not new:
            return
        logger.info(
            f"[tg2qq] @{real_channel}: {len(new)} new message(s) "
            f"(id {new[0]['id']}..{new[-1]['id']})"
        )
        await self._push(real_channel, title, new)
        # Advance the baseline regardless of push results (agreed behavior);
        # failures are logged inside _push and never retried.
        self.baseline[real_channel] = new[-1]["id"]
        self._save_baseline()

    def _find_qq_platform(self):
        """Find the first aiocqhttp platform instance.

        Returns:
            The platform instance, or None if no aiocqhttp platform is loaded.
        """
        for inst in self.context.platform_manager.platform_insts:
            if inst.meta().name == "aiocqhttp":
                return inst
        return None

    async def _resolve_groups(self, platform) -> list[str]:
        """Resolve the target group ids for a push.

        Args:
            platform: The aiocqhttp platform instance.

        Returns:
            Whitelisted group ids, or all groups of the bot when the whitelist
            is empty; empty when the group list cannot be fetched.
        """
        if self.whitelist:
            return self.whitelist
        try:
            group_list = await platform.get_client().call_action("get_group_list")
        except Exception:
            logger.exception("[tg2qq] get_group_list failed")
            return []
        groups = []
        for g in group_list:
            gid = str(g.get("group_id") or g.get("gid") or "").strip()
            if gid:
                groups.append(gid)
        return groups

    def _filter_text(self, text: str, exempt: bool) -> str:
        """Apply the content filter regex to one pushed text block.

        Args:
            text: A text block (body, reply, link or video placeholder).
            exempt: True when the message matched exclude_regex and must not
                be filtered.

        Returns:
            The text with filter matches removed; when a removal happens,
            lines emptied by it are dropped.
        """
        if exempt or not self.filter_res:
            return text
        filtered = text
        for pattern in self.filter_res:
            filtered = pattern.sub("", filtered)
        if filtered == text:
            return text
        filtered = re.sub(r"[ \t]{2,}", " ", filtered)
        lines = [line.strip() for line in filtered.split("\n")]
        return "\n".join(line for line in lines if line)

    def _build_node(
        self, channel: str, title: str | None, msg: dict, exempt: bool
    ) -> Node:
        """Build one forward node from a parsed Telegram message.

        Args:
            channel: The real channel username (used for the post link).
            title: The channel display title, used as the node sender name.
            msg: A message dict produced by parse_page.
            exempt: True when the message matched exclude_regex; the content
                filter is skipped for it.

        Returns:
            A Node whose content is the post time, text, links, video
            placeholder and images.
        """
        name = title or f"@{channel}"
        post_url = f"https://t.me/{channel}/{msg['id']}"
        parts: list[str] = []
        if msg.get("date"):
            parts.append(f"{msg['date'][:16].replace('T', ' ')} UTC")
        if msg.get("reply"):
            parts.append(self._filter_text(f"↩ {msg['reply']}", exempt))
        if msg.get("text"):
            parts.append(self._filter_text(msg["text"], exempt))
        for link in msg.get("links", []):
            link = self._filter_text(link, exempt)
            if link:
                parts.append(link)
        if msg.get("video"):
            placeholder = self._filter_text(f"▶ 视频见原文: {post_url}", exempt)
            # When the filter stripped the URL the line ends with a bare colon.
            placeholder = placeholder.rstrip(":").rstrip()
            if placeholder:
                parts.append(placeholder)
        content: list = []
        if parts:
            content.append(Plain("\n".join(parts)))
        for url in msg.get("images", []):
            content.append(Image.fromURL(url))
        if not content:
            content.append(Plain(post_url))
        return Node(uin="10000", name=name, content=content)

    async def _push(self, channel: str, title: str | None, messages: list[dict]):
        """Push parsed messages to every target group as one forward message.

        Args:
            channel: The real channel username.
            title: The channel display title.
            messages: New messages sorted by id ascending.
        """
        platform = self._find_qq_platform()
        if platform is None:
            logger.error("[tg2qq] no aiocqhttp platform found, skip push")
            return
        groups = await self._resolve_groups(platform)
        if not groups:
            logger.warning("[tg2qq] no target group resolved, skip push")
            return
        platform_id = platform.meta().id
        entries: list[tuple[dict, bool]] = []
        skipped = 0
        blocked = 0
        for m in messages:
            text = m.get("text") or ""
            exempt = any(p.search(text) for p in self.exclude_res)
            if not exempt and any(p.search(text) for p in self.skip_res):
                skipped += 1
                continue
            if self.safety is not None and text:
                # The strategies use blocking HTTP (Baidu SDK), so run them in a
                # worker thread; fail open on errors since the baseline moves on
                # regardless and a moderation outage should not mute the channel.
                try:
                    ok, info = await asyncio.to_thread(self.safety.check, text)
                except Exception:
                    logger.exception(
                        "[tg2qq] content safety check errored, message allowed"
                    )
                else:
                    if not ok:
                        blocked += 1
                        logger.info(
                            f"[tg2qq] @{channel}: message {m['id']} blocked by "
                            f"content safety: {info}"
                        )
                        continue
            entries.append((m, exempt))
        if blocked:
            logger.info(
                f"[tg2qq] @{channel}: {blocked} message(s) blocked by content safety"
            )
        if skipped:
            logger.info(
                f"[tg2qq] @{channel}: {skipped} message(s) skipped by skip_push_regex"
            )
        if not entries:
            logger.info(f"[tg2qq] @{channel}: nothing left to push after skipping")
            return
        chain = MessageChain(
            chain=[
                Nodes(
                    [
                        self._build_node(channel, title, m, exempt)
                        for m, exempt in entries
                    ]
                )
            ]
        )
        for group in groups:
            try:
                sent = await self.context.send_message(
                    f"{platform_id}:GroupMessage:{group}", chain
                )
                if sent:
                    logger.info(f"[tg2qq] pushed to group {group}")
                else:
                    logger.warning(f"[tg2qq] push to group {group} was not sent")
            except Exception:
                logger.exception(f"[tg2qq] push to group {group} failed")
            if self.send_interval > 0:
                await asyncio.sleep(self.send_interval)
        logger.info(
            f"[tg2qq] @{channel}: forwarded {len(entries)} message(s) to {len(groups)} group(s)"
        )
