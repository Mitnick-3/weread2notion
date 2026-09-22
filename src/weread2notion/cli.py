import argparse
import hashlib
import logging
import os
import re
import time
from datetime import datetime

import requests
from dotenv import load_dotenv
from notion_client import Client
from notion_client.errors import APIResponseError
from retrying import retry

from .blocks import (
    get_callout,
    get_date,
    get_heading,
    get_icon,
    get_multi_select,
    get_number,
    get_quote,
    get_rich_text,
    get_select,
    get_status,
    get_title,
    get_url,
)

client = None
data_source_id = None
data_source_property_types = {}
data_source_property_configs = {}
title_property_name = None
skipped_property_names = set()
weread = None

load_dotenv()

WEREAD_URL = "https://weread.qq.com/"
WEREAD_GATEWAY_URL = "https://i.weread.qq.com/api/agent/gateway"
WEREAD_SKILL_VERSION = "1.0.4"

# Notion 当前 API 版本
NOTION_VERSION = "2026-03-11"

BOOKMARK_CALLOUT_ICON = "〰️"
NOTE_CALLOUT_ICON = "✍️"

NOTION_TOKEN_PATTERN = re.compile(
    r"^(secret|ntn)_[A-Za-z0-9_-]{20,}$"
)
WEREAD_API_KEY_PATTERN = re.compile(
    r"^[A-Za-z0-9._~+/=-]{10,}$"
)
NOTION_ID_PATTERN = re.compile(
    r"^[a-f0-9]{32}$|^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$",
    re.IGNORECASE,
)
NOTION_ID_IN_TEXT_PATTERN = re.compile(
    r"([a-f0-9]{32}|[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})",
    re.IGNORECASE,
)


class ConfigError(Exception):
    pass


def emit_error(message):
    if os.getenv("GITHUB_ACTIONS") == "true":
        safe = (
            message.replace("%", "%25")
            .replace("\r", "%0D")
            .replace("\n", "%0A")
        )
        print(f"::error::{safe}")
    else:
        print(f"配置错误: {message}")


def fail_config(message):
    emit_error(message)
    raise ConfigError(message)


def clean_secret_value(name, required=False):
    raw = os.getenv(name)

    if raw is None:
        if required:
            fail_config(
                f"缺少 {name}，请在 GitHub Actions Secrets 中配置"
            )
        return None

    value = re.sub(r"\s+", "", raw)

    if value:
        os.environ[name] = value
        return value

    if required:
        fail_config(f"{name} 为空，请检查 GitHub Actions Secrets")

    os.environ.pop(name, None)
    return None


def validate_regex(name, value, pattern, hint):
    if value and not pattern.search(value):
        fail_config(f"{name} 格式不正确：{hint}")
    return value


def validate_secret_inputs():
    weread_api_key = clean_secret_value(
        "WEREAD_API_KEY",
        required=True,
    )
    notion_token = clean_secret_value(
        "NOTION_TOKEN",
        required=True,
    )

    notion_page = clean_secret_value("NOTION_PAGE")
    notion_database_id = clean_secret_value("NOTION_DATABASE_ID")
    notion_data_source_id = clean_secret_value(
        "NOTION_DATA_SOURCE_ID"
    )

    validate_regex(
        "WEREAD_API_KEY",
        weread_api_key,
        WEREAD_API_KEY_PATTERN,
        "应为微信读书 Gateway API Key，不能包含空格或换行",
    )

    validate_regex(
        "NOTION_TOKEN",
        notion_token,
        NOTION_TOKEN_PATTERN,
        "应以 secret_ 或 ntn_ 开头，不能包含空格或换行",
    )

    for name, value in (
        ("NOTION_DATA_SOURCE_ID", notion_data_source_id),
        ("NOTION_DATABASE_ID", notion_database_id),
    ):
        validate_regex(
            name,
            value,
            NOTION_ID_PATTERN,
            "应为 32 位 Notion ID 或带连字符的 UUID",
        )

    if notion_page and not NOTION_ID_IN_TEXT_PATTERN.search(
        notion_page
    ):
        fail_config(
            "NOTION_PAGE 格式不正确：请填写 Notion 页面链接、数据库链接或 ID"
        )

    if not (
        notion_data_source_id
        or notion_page
        or notion_database_id
    ):
        fail_config(
            "缺少 NOTION_PAGE / NOTION_DATA_SOURCE_ID / "
            "NOTION_DATABASE_ID，请至少配置其中一个"
        )

    return {
        "weread_api_key": weread_api_key,
        "notion_token": notion_token,
    }


class WeReadGatewayClient:
    def __init__(self, api_key):
        if not api_key:
            fail_config(
                "没有找到 WEREAD_API_KEY，请在 GitHub Actions Secrets 中配置"
            )

        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
        )

    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def request(self, api_name, **kwargs):
        payload = {
            "api_name": api_name,
            "skill_version": WEREAD_SKILL_VERSION,
            **kwargs,
        }

        response = self.session.post(
            WEREAD_GATEWAY_URL,
            json=payload,
            timeout=30,
        )

        response.raise_for_status()

        data = response.json()

        if data.get("upgrade_info"):
            raise Exception(
                f"微信读书 skill 需要升级: "
                f"{data.get('upgrade_info')}"
            )

        if data.get("errcode", 0) != 0:
            raise Exception(
                f"微信读书 Gateway 请求失败: "
                f"{api_name}, "
                f"errcode={data.get('errcode')}, "
                f"response={data}"
            )

        return data


def get_range_start(item):
    note_range = item.get("range") or ""

    try:
        return int(note_range.split("-")[0] or 0)
    except (ValueError, TypeError):
        return 0


def get_note_sort_key(item, chapter=None):
    chapter_uid = item.get("chapterUid", 1)
    chapter_info = None

    if chapter:
        chapter_info = (
            chapter.get(chapter_uid)
            or chapter.get(str(chapter_uid))
        )

    chapter_idx = (
        chapter_info.get("chapterIdx", 1000000)
        if chapter_info
        else chapter_uid
    )

    return (
        chapter_idx,
        get_range_start(item),
    )


@retry(stop_max_attempt_number=3, wait_fixed=5000)
def get_bookmark_list(bookId):
    """获取我的划线"""
    data = weread.request(
        "/book/bookmarklist",
        bookId=bookId,
    )

    updated = (
        data.get("updated")
        or data.get("items")
        or []
    )

    if not isinstance(updated, list):
        updated = []

    # /book/bookmarklist 的正式返回字段是 updated[].markText。
    # 某些网关版本可能把内容放在 items 中，兼容两种结构。
    normalized = []
    for item in updated:
        if not isinstance(item, dict):
            continue
        item = dict(item)
        if not item.get("markText"):
            item["markText"] = (
                item.get("text")
                or item.get("content")
                or ""
            )
        normalized.append(item)

    return sorted(
        normalized,
        key=get_note_sort_key,
    )


@retry(stop_max_attempt_number=3, wait_fixed=5000)
def get_read_info(bookId):
    data = weread.request(
        "/book/getprogress",
        bookId=bookId,
    )

    book = data.get("book") or {}

    progress = to_number(
        book.get("progress")
    ) or 0

    reading_progress = normalize_reading_progress(
        progress
    )

    finish_time = (
        book.get("finishTime")
        or book.get("finishReadingTime")
        or 0
    )
    update_time = (
        book.get("updateTime")
        or book.get("readUpdateTime")
        or 0
    )

    # 微信读书 getprogress 的累计阅读时长字段是 readingTime。
    # recordReadingTime 在部分返回中只是记录/上报字段，不能作为总阅读时长。
    reading_time = (
        book.get("readingTime")
        or book.get("totalReadTime")
        or book.get("recordReadingTime")
        or 0
    )

    if finish_time or progress >= 100:
        marked_status = 4
    elif (
        update_time
        or book.get("isStartReading")
        or progress > 0
    ):
        marked_status = 2
    else:
        marked_status = 1

    return {
        "markedStatus": marked_status,
        "readingTime": reading_time,
        "readingProgress": reading_progress,
        # “时间”字段同步最近一次阅读时间；
        # 如果已经读完，则优先使用完成时间。
        "readingDate": finish_time or update_time,
        "finishedDate": finish_time,
    }


def normalize_reading_progress(value):
    value = to_number(value) or 0

    if value > 1:
        value = value / 100

    return round(
        min(max(value, 0), 1),
        4,
    )


def normalize_rating(value):
    value = value or 0

    if value > 100:
        return value / 1000

    if value > 10:
        return value / 10

    return value


@retry(stop_max_attempt_number=3, wait_fixed=5000)
def get_bookinfo(bookId):
    """获取书的详情"""
    data = weread.request(
        "/book/info",
        bookId=bookId,
    )

    isbn = data.get("isbn", "")
    newRating = normalize_rating(
        data.get("newRating")
    )

    # 微信读书 /book/info 返回 publishTime，例如：
    # "2025-04-01 00:00:00"。
    # Notion 的“年份”字段使用出版年份（整数）同步。
    publish_time = data.get("publishTime") or ""
    year = None
    if isinstance(publish_time, str):
        value = publish_time.strip()
        match = re.search(r"(\d{4})", value)
        if match:
            year = int(match.group(1))
    elif isinstance(publish_time, (int, float)):
        try:
            year = datetime.utcfromtimestamp(publish_time).year
        except (TypeError, ValueError, OSError, OverflowError):
            year = None

    return (
        isbn,
        newRating,
        year,
    )


@retry(stop_max_attempt_number=3, wait_fixed=5000)
def get_review_list(bookId):
    """获取笔记"""
    reviews_data = []
    hasMore = 1
    synckey = 0

    while hasMore:
        data = weread.request(
            "/review/list/mine",
            bookid=bookId,
            synckey=synckey,
            count=100,
        )

        hasMore = data.get("hasMore", 0)
        synckey = data.get("synckey", 0)

        batch = data.get("reviews") or []
        reviews_data.extend(batch)

        if not batch:
            hasMore = 0

    summary = list(
        filter(
            lambda x: (
                x.get("review") or {}
            ).get("type") == 4,
            reviews_data,
        )
    )

    reviews = list(
        filter(
            lambda x: (
                x.get("review") or {}
            ).get("type") == 1,
            reviews_data,
        )
    )

    reviews = list(
        map(
            lambda x: x.get("review") or {},
            reviews,
        )
    )

    normalized_reviews = []

    for item in reviews:
        item = dict(item)

        item["markText"] = item.pop(
            "content",
            "",
        )

        item["_callout_icon"] = NOTE_CALLOUT_ICON

        normalized_reviews.append(item)

    return summary, normalized_reviews


@retry(stop_max_attempt_number=3, wait_fixed=5000)
def get_chapter_info(bookId):
    """获取章节信息"""
    data = weread.request(
        "/book/chapterinfo",
        bookId=bookId,
    )

    chapters = data.get("chapters") or []

    return {
        item["chapterUid"]: item
        for item in chapters
        if "chapterUid" in item
    }


def get_notebooklist():
    """
    获取微信读书完整书架列表。

    旧版本使用 /user/notebooks。
    但 /user/notebooks 的语义是“所有有笔记的书”，
    因此没有划线/笔记的书会从源头被过滤掉。

    这里改用 /shelf/sync 获取完整电子书书架。
    阅读进度、阅读时长、是否读完等信息再由
    /book/getprogress 按书单独获取。
    """
    data = weread.request(
        "/shelf/sync",
    )

    shelf_books = data.get("books") or []

    books = []

    for index, item in enumerate(shelf_books):
        if not isinstance(item, dict):
            continue

        book_id = item.get("bookId")

        if not book_id:
            continue

        # /shelf/sync 返回的是扁平书籍对象，
        # 而主同步流程兼容旧的 {book: {...}, sort: ...} 结构。
        book = dict(item)

        # 保留原有排序字段，避免改变 Notion 中的 Sort 逻辑。
        # 书架接口没有 /user/notebooks 的 note-sort，
        # 因此这里使用书架顺序作为稳定排序。
        book["sort"] = (
            item.get("sort")
            or item.get("updateTime")
            or item.get("readUpdateTime")
            or index
        )

        # 主流程原本从 book["categories"] 读取分类。
        # /shelf/sync 返回的是 category 字符串，因此转换成兼容结构。
        category = item.get("category")
        if category:
            book["categories"] = [
                {"title": category}
            ]
        else:
            book["categories"] = []

        books.append(
            {
                "book": book,
                "sort": book["sort"],
            }
        )

    books.sort(
        key=lambda x: x.get("sort") or 0
    )

    return books


def extract_notion_id():
    url_or_id = (
        os.getenv("NOTION_DATA_SOURCE_ID")
        or os.getenv("NOTION_PAGE")
        or os.getenv("NOTION_DATABASE_ID")
    )

    if not url_or_id:
        fail_config(
            "没有找到 NOTION_PAGE / NOTION_DATA_SOURCE_ID，请按照文档填写"
        )

    match = NOTION_ID_IN_TEXT_PATTERN.search(
        url_or_id
    )

    if match:
        return match.group(0)

    fail_config(
        "获取 Notion ID 失败，请检查 "
        "NOTION_PAGE / NOTION_DATA_SOURCE_ID"
    )


def query_data_source(**body):
    return client.request(
        path=f"data_sources/{data_source_id}/query",
        method="POST",
        body=body,
    )


def load_data_source_schema():
    """读取当前 data source 的真实属性。"""
    global data_source_property_types
    global data_source_property_configs
    global title_property_name
    global skipped_property_names

    response = client.request(
        path=f"data_sources/{data_source_id}",
        method="GET",
    )

    properties = response.get("properties") or {}

    data_source_property_configs = properties
    data_source_property_types = {
        name: (config or {}).get("type")
        for name, config in properties.items()
    }

    title_property_name = next(
        (
            name
            for name, prop_type
            in data_source_property_types.items()
            if prop_type == "title"
        ),
        None,
    )

    skipped_property_names = set()

    if not title_property_name:
        raise Exception(
            "Notion data source 缺少标题属性，请保留一个 Title 类型属性"
        )

    missing = [
        name
        for name in ("BookId", "Sort")
        if name not in data_source_property_types
    ]

    if missing:
        raise Exception(
            f"Notion data source 缺少必填属性: "
            f"{', '.join(missing)}。"
            "请在模板中补充后重试"
        )

    print(
        f"已读取 Notion 属性 "
        f"{len(data_source_property_types)} 个，"
        f"标题属性: {title_property_name}"
    )


def ensure_year_property():
    """确保 Notion 数据源存在“年份”字段，并且使用 Number 类型。"""
    global data_source_property_types
    global data_source_property_configs

    if "年份" in data_source_property_types:
        prop_type = data_source_property_types.get("年份")
        if prop_type != "number":
            print(
                f"警告：Notion 的“年份”字段类型是 {prop_type}，"
                "不是 number，无法写入出版年份。请将该字段改为数字。"
            )
        return

    try:
        client.request(
            path=f"data_sources/{data_source_id}",
            method="PATCH",
            body={
                "properties": {
                    "年份": {"number": {}}
                }
            },
        )
        print("已自动在 Notion 数据源中补充“年份”字段（Number）。")
        load_data_source_schema()
    except Exception as error:
        print(f"警告：自动创建 Notion“年份”字段失败：{error}")


def get_property_type(name):
    return data_source_property_types.get(name)


def has_any_property(names):
    return any(
        name in data_source_property_types
        for name in names
    )


def build_equals_filter(name, value):
    prop_type = get_property_type(name)

    if prop_type in {
        "title",
        "rich_text",
        "url",
        "email",
        "phone_number",
    }:
        return {
            "property": name,
            prop_type: {
                "equals": str(value),
            },
        }

    if prop_type == "number":
        return {
            "property": name,
            "number": {
                "equals": to_number(value),
            },
        }

    if prop_type == "select":
        return {
            "property": name,
            "select": {
                "equals": str(value),
            },
        }

    if prop_type == "status":
        return {
            "property": name,
            "status": {
                "equals": str(value),
            },
        }

    raise Exception(
        f"Notion 属性 {name} 的类型 {prop_type} "
        "暂不支持用于查询"
    )


def build_is_not_empty_filter(name):
    prop_type = get_property_type(name)

    if prop_type in {
        "title",
        "rich_text",
        "url",
        "email",
        "phone_number",
        "number",
        "select",
        "status",
        "date",
    }:
        return {
            "property": name,
            prop_type: {
                "is_not_empty": True,
            },
        }

    raise Exception(
        f"Notion 属性 {name} 的类型 {prop_type} "
        "暂不支持用于查询"
    )


def to_text(value):
    if value is None:
        return ""

    if isinstance(value, (list, tuple)):
        return ", ".join(
            to_text(item)
            for item in value
            if item is not None
        )

    return str(value)


def to_name_list(value):
    if value is None:
        return []

    if isinstance(value, (list, tuple, set)):
        return [
            to_text(item)
            for item in value
            if to_text(item)
        ]

    text = to_text(value)

    return [text] if text else []


def to_number(value):
    if value is None or value == "":
        return None

    if isinstance(value, (int, float)):
        return value

    try:
        number = float(value)
    except (TypeError, ValueError):
        return None

    return (
        int(number)
        if number.is_integer()
        else number
    )


def normalize_date_value(value):
    if isinstance(value, (int, float)):
        return datetime.utcfromtimestamp(
            value
        ).strftime("%Y-%m-%d %H:%M:%S")

    return value


def get_status_option_names(name):
    config = data_source_property_configs.get(name) or {}
    status_config = config.get("status") or {}
    options = status_config.get("options") or []
    return {str(item.get("name")) for item in options if item.get("name")}


def build_option_property(prop_type, value, property_name=None):
    names = to_name_list(value)

    if not names:
        return None

    if prop_type == "status":
        option_name = names[0]
        if property_name:
            allowed = get_status_option_names(property_name)
            if allowed and option_name not in allowed:
                return None
        return get_status(option_name)

    if prop_type == "select":
        return get_select(names[0])

    return get_multi_select(names)


def build_notion_property(name, value):
    prop_type = get_property_type(name)

    if not prop_type:
        if name not in skipped_property_names:
            print(
                f"属性 {name} 在 Notion 模板中不存在，自动跳过"
            )
            skipped_property_names.add(name)

        return None

    if value is None:
        return None

    if prop_type == "title":
        return get_title(to_text(value))

    if prop_type == "rich_text":
        return get_rich_text(to_text(value))

    if prop_type == "number":
        number = to_number(value)

        return (
            get_number(number)
            if number is not None
            else None
        )

    if prop_type == "url":
        return get_url(to_text(value))

    if prop_type in {
        "multi_select",
        "status",
        "select",
    }:
        return build_option_property(
            prop_type,
            value,
            property_name=name,
        )

    if prop_type == "date":
        return get_date(
            normalize_date_value(value)
        )

    if prop_type == "checkbox":
        return {
            "checkbox": bool(value),
        }

    if name not in skipped_property_names:
        print(
            f"属性 {name} 的类型 {prop_type} "
            "暂不支持写入，自动跳过"
        )
        skipped_property_names.add(name)

    return None


def build_notion_properties(raw_properties):
    return {
        name: prop
        for name, value in raw_properties.items()
        if (
            prop := build_notion_property(
                name,
                value,
            )
        ) is not None
    }


def get_number_property_value(property_value):
    if not property_value:
        return 0

    prop_type = property_value.get("type")
    value = property_value.get(prop_type)

    if prop_type == "number":
        return value or 0

    if prop_type in {
        "title",
        "rich_text",
    } and value:
        return (
            to_number(
                value[0].get("plain_text")
            )
            or 0
        )

    if prop_type in {
        "select",
        "status",
    } and value:
        return (
            to_number(
                value.get("name")
            )
            or 0
        )

    return 0


def resolve_data_source_id(notion_id):
    if os.getenv("NOTION_DATA_SOURCE_ID"):
        return notion_id

    try:
        client.request(
            path=f"data_sources/{notion_id}",
            method="GET",
        )

        return notion_id

    except APIResponseError as error:
        code = getattr(
            error.code,
            "value",
            error.code,
        )

        if code not in {
            "object_not_found",
            "validation_error",
        }:
            raise

    database = client.request(
        path=f"databases/{notion_id}",
        method="GET",
    )

    sources = database.get(
        "data_sources"
    ) or []

    if not sources:
        raise Exception(
            f"数据库 {notion_id} 下没有可用的 data source"
        )

    if len(sources) > 1:
        print(
            f"数据库 {notion_id} 包含 "
            f"{len(sources)} 个 data sources，"
            f"默认使用第一个: "
            f"{sources[0].get('id')}"
        )

    return sources[0]["id"]


# =========================================================
# Notion 增量同步
# =========================================================

def find_existing_book(book_id):
    """
    根据 BookId 查找已经同步到 Notion 的书籍页面。

    不再删除旧页面。
    """
    filter_body = build_equals_filter(
        "BookId",
        book_id,
    )

    response = query_data_source(
        filter=filter_body,
        page_size=10,
    )

    results = response.get("results") or []

    if not results:
        return None

    return results[0]


def get_page_children(page_id):
    """
    获取 Notion 页面下全部一级 block。
    """
    results = []
    start_cursor = None

    while True:
        body = {
            "page_size": 100,
        }

        if start_cursor:
            body["start_cursor"] = start_cursor

        response = client.blocks.children.list(
            block_id=page_id,
            **body,
        )

        results.extend(
            response.get("results") or []
        )

        if not response.get("has_more"):
            break

        start_cursor = response.get(
            "next_cursor"
        )

        if not start_cursor:
            break

    return results


def get_block_text(block):
    """
    提取 Notion block 的纯文本。
    支持 heading / callout / quote 等 rich_text block。
    """
    block_type = block.get("type")

    if not block_type:
        return ""

    data = block.get(block_type) or {}

    rich_text = data.get(
        "rich_text"
    ) or []

    return "".join(
        item.get("plain_text") or ""
        for item in rich_text
    ).strip()


def normalize_dedupe_text(text):
    """
    用于去重的文本标准化。

    不改变实际写入 Notion 的文本，
    只用于判断两条内容是不是相同。
    """
    if not text:
        return ""

    text = str(text)

    text = text.replace(
        "\r\n",
        "\n",
    ).replace(
        "\r",
        "\n",
    )

    text = re.sub(
        r"[ \t]+",
        " ",
        text,
    )

    return text.strip()


def get_block_dedupe_key(block):
    """
    生成 block 去重 Key。

    heading / callout / quote 会带 block 类型，
    防止同样的文字因为类型不同而互相误判。
    """
    block_type = block.get("type")

    if block_type not in {
        "heading_1",
        "heading_2",
        "heading_3",
        "callout",
        "quote",
    }:
        return None

    text = normalize_dedupe_text(
        get_block_text(block)
    )

    if not text:
        return None

    return (
        block_type,
        hashlib.sha1(
            text.encode("utf-8")
        ).hexdigest(),
    )


def get_existing_block_keys(page_id):
    """
    获取 Notion 页面已有内容的去重 Key。
    """
    blocks = get_page_children(page_id)

    keys = set()

    for block in blocks:
        key = get_block_dedupe_key(block)

        if key:
            keys.add(key)

    return keys


def filter_new_children(children, existing_keys):
    """
    只保留 Notion 中还没有的 block。

    同一批次中也会去重，避免微信读书返回重复内容。
    """
    result = []
    batch_keys = set()

    for child in children:
        key = get_block_dedupe_key(child)

        # 非内容 block 默认保留
        if key is None:
            result.append(child)
            continue

        if key in existing_keys:
            continue

        if key in batch_keys:
            continue

        batch_keys.add(key)
        result.append(child)

    return result


def add_children(page_id, children):
    """
    向 Notion 追加 block。

    保留这个函数名，避免和旧版本代码产生 NameError。
    """
    if not children:
        return []

    results = []

    for i in range(
        0,
        len(children),
        100,
    ):
        batch = children[
            i:i + 100
        ]

        time.sleep(0.3)

        response = client.blocks.children.append(
            block_id=page_id,
            children=batch,
        )

        results.extend(
            response.get("results") or []
        )

    return results


def format_reading_time(seconds):
    seconds = int(to_number(seconds) or 0)
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60

    parts = []
    if hours > 0:
        parts.append(f"{hours}时")
    if minutes > 0:
        parts.append(f"{minutes}分")

    return "".join(parts) or "0分"


def build_book_raw_properties(
    bookName,
    bookId,
    sort,
    author,
    isbn,
    rating,
    categories,
    year=None,
    read_info=None,
):
    """
    创建/更新书籍时统一使用这一套属性。

    关键点：
    - 书籍属性同步与划线/笔记同步完全独立。
    - 每次 sync 都更新阅读进度。
    - markedStatus:
        1 = 未开始
        2 = 在读
        4 = 读完
    - 如果 Notion 没有“未读”这个 status option，未开始时不写非法 option。
    """
    raw_properties = {
        title_property_name: bookName,
        "BookId": bookId,
        "ISBN": isbn,
        "链接": (
            "https://weread.qq.com/web/reader/"
            f"{calculate_book_str_id(bookId)}"
        ),
        "作者": author,
        "Sort": sort,
        "评分": rating,
        "年份": year,
    }

    if categories is not None:
        raw_properties["分类"] = categories

    if read_info is not None:
        marked_status = read_info.get("markedStatus", 1)
        reading_time = read_info.get("readingTime", 0)
        reading_progress = read_info.get("readingProgress", 0)
        finished_date = read_info.get("finishedDate") or 0

        if marked_status == 4:
            raw_properties["状态"] = "读完"
        elif marked_status == 2:
            raw_properties["状态"] = "在读"
        else:
            # 不假设 Notion 中存在“未读”选项。
            # 如果模板没有“未读”，build_notion_property 会跳过它。
            if "未读" in get_status_option_names("状态"):
                raw_properties["状态"] = "未读"
            else:
                # 不发送非法 status option。
                # 这样不会因为一个“未开始”的书导致整本书属性更新失败。
                raw_properties.pop("状态", None)

        raw_properties["阅读时长"] = format_reading_time(reading_time)
        raw_properties["阅读进度"] = reading_progress

        reading_date = (
            read_info.get("readingDate")
            or finished_date
        )
        if reading_date:
            raw_properties["时间"] = datetime.utcfromtimestamp(
                reading_date
            ).strftime("%Y-%m-%d")

    return raw_properties


def insert_to_notion(
    bookName,
    bookId,
    cover,
    sort,
    author,
    isbn,
    rating,
    categories,
    year=None,
    read_info=None,
):
    """创建新的 Notion 书籍页面。"""

    if not cover or not cover.startswith("http"):
        cover = "https://www.notion.so/icons/book_gray.svg"

    parent = {
        "type": "data_source_id",
        "data_source_id": data_source_id,
    }

    if read_info is None and has_any_property(
        ("状态", "阅读时长", "阅读进度", "时间")
    ):
        read_info = get_read_info(bookId)

    raw_properties = build_book_raw_properties(
        bookName=bookName,
        bookId=bookId,
        sort=sort,
        author=author,
        isbn=isbn,
        rating=rating,
        categories=categories,
        year=year,
        read_info=read_info,
    )

    properties = build_notion_properties(raw_properties)
    icon = get_icon(cover)

    response = client.pages.create(
        parent=parent,
        icon=icon,
        cover=icon,
        properties=properties,
    )

    return response["id"]


def update_existing_book_properties(
    page_id,
    bookName,
    bookId,
    cover,
    sort,
    author,
    isbn,
    rating,
    categories,
    year=None,
    read_info=None,
):
    """
    更新已经存在的书籍属性.

    不删除页面，不重建页面，不依赖是否存在划线/笔记。
    所以即使一本书“没有划线、没有笔记、还没读完”，
    它的阅读进度/状态/阅读时长仍然会更新。
    """
    if read_info is None and has_any_property(
        ("状态", "阅读时长", "阅读进度", "时间")
    ):
        read_info = get_read_info(bookId)

    raw_properties = build_book_raw_properties(
        bookName=bookName,
        bookId=bookId,
        sort=sort,
        author=author,
        isbn=isbn,
        rating=rating,
        categories=categories,
        year=year,
        read_info=read_info,
    )

    properties = build_notion_properties(raw_properties)

    if properties:
        client.pages.update(
            page_id=page_id,
            properties=properties,
        )


def sync_book_content(page_id, book_id, title, existing_keys=None):
    """
    单独负责划线/笔记增量同步。

    书籍本身是否有划线，不影响前面的书籍属性同步。
    """
    if existing_keys is None:
        existing_keys = get_existing_block_keys(page_id)

    bookmark_list = get_bookmark_list(book_id)
    summary, reviews = get_review_list(book_id)

    print(
        f"    → 微信读书内容："
        f"划线 {len(bookmark_list)} 条，"
        f"笔记 {len(reviews)} 条，"
        f"点评 {len(summary)} 条"
    )

    # 没有任何内容时，直接结束。
    if not bookmark_list and not reviews and not summary:
        print(f"    → 《{title}》没有划线或笔记，书籍信息已同步")
        return 0

    # 只有确实存在内容时才请求章节，减少 API 请求。
    chapter = get_chapter_info(book_id)

    bookmark_list = list(bookmark_list)
    bookmark_list.extend(reviews)
    bookmark_list.sort(
        key=lambda x: get_note_sort_key(x, chapter)
    )

    children = get_children(
        chapter,
        summary,
        bookmark_list,
    )

    if not children:
        print(f"    → 《{title}》没有可写入的内容")
        return 0

    new_children = filter_new_children(
        children,
        existing_keys,
    )

    if not new_children:
        print(f"    → 《{title}》没有新的划线/笔记")
        return 0

    results = add_children(
        page_id,
        new_children,
    )

    print(
        f"    → 《{title}》新增 "
        f"{len(results)} 个内容块"
    )
    return len(results)


# =========================================================
# 微信读书内容转换
# =========================================================

def get_ancestor_chain(
    current_chapter_info,
    all_chapters,
):
    if not current_chapter_info:
        return []

    try:
        current_pos = all_chapters.index(
            current_chapter_info
        )
    except ValueError:
        return [current_chapter_info]

    chain = []

    target_level = current_chapter_info.get(
        "level",
        1,
    )

    for index in range(
        current_pos - 1,
        -1,
        -1,
    ):
        candidate = all_chapters[index]

        candidate_level = candidate.get(
            "level",
            1,
        )

        if candidate_level < target_level:
            chain.insert(
                0,
                candidate,
            )

            target_level = candidate_level

            if target_level <= 1:
                break

    chain.append(
        current_chapter_info
    )

    return chain


def get_children(
    chapter,
    summary,
    bookmark_list,
):
    """
    把微信读书划线、笔记转换成 Notion blocks。

    每条划线/笔记最多拆成 2000 字符一个 callout。
    """
    children = []

    all_chapters = []

    if chapter:
        for uid, info in chapter.items():
            item = dict(info)

            item["chapterUid"] = item.get(
                "chapterUid",
                uid,
            )

            all_chapters.append(item)

        all_chapters.sort(
            key=lambda x: x.get(
                "chapterIdx",
                0,
            )
        )

    chapter_nodes = {
        node.get("chapterUid"): node
        for node in all_chapters
    }

    if chapter:
        grouped_bookmarks = []

        last_uid = None
        current_group = None

        for data in bookmark_list:
            uid = data.get(
                "chapterUid",
                1,
            )

            if uid != last_uid:
                if current_group:
                    grouped_bookmarks.append(
                        current_group
                    )

                info = (
                    chapter.get(uid)
                    or chapter.get(str(uid))
                )

                current_group = {
                    "chapterUid": uid,
                    "bookmarks": [],
                    "chapterInfo": info,
                }

                last_uid = uid

            current_group[
                "bookmarks"
            ].append(data)

        if current_group:
            grouped_bookmarks.append(
                current_group
            )

        previous_path_uids = []

        for group in grouped_bookmarks:
            info = group["chapterInfo"]

            if info:
                current_info = (
                    chapter_nodes.get(
                        group["chapterUid"]
                    )
                    or chapter_nodes.get(
                        str(group["chapterUid"])
                    )
                )

                if current_info is None:
                    current_info = dict(info)

                    current_info[
                        "chapterUid"
                    ] = current_info.get(
                        "chapterUid",
                        group["chapterUid"],
                    )

                path = get_ancestor_chain(
                    current_info,
                    all_chapters,
                )

                divergence_index = 0

                min_len = min(
                    len(path),
                    len(previous_path_uids),
                )

                while (
                    divergence_index < min_len
                ):
                    path_uid = path[
                        divergence_index
                    ].get("chapterUid")

                    if (
                        path_uid
                        != previous_path_uids[
                            divergence_index
                        ]
                    ):
                        break

                    divergence_index += 1

                for chapter_node in path[
                    divergence_index:
                ]:
                    level = chapter_node.get(
                        "level",
                        1,
                    )

                    # Notion heading 最多支持三级
                    level = min(
                        max(
                            int(level or 1),
                            1,
                        ),
                        3,
                    )

                    children.append(
                        get_heading(
                            level,
                            chapter_node.get(
                                "title"
                            ),
                        )
                    )

                previous_path_uids = [
                    node.get("chapterUid")
                    for node in path
                ]

            else:
                previous_path_uids = []

            for item in group[
                "bookmarks"
            ]:
                markText = (
                    item.get("markText")
                    or ""
                )

                if not markText:
                    continue

                callout_icon = (
                    item.get("_callout_icon")
                    or BOOKMARK_CALLOUT_ICON
                )

                for start in range(
                    0,
                    len(markText),
                    2000,
                ):
                    children.append(
                        get_callout(
                            markText[
                                start:start + 2000
                            ],
                            icon=callout_icon,
                        )
                    )

                abstract = item.get(
                    "abstract"
                )

                if abstract:
                    children.append(
                        get_quote(abstract)
                    )

    else:
        for data in bookmark_list:
            markText = (
                data.get("markText")
                or ""
            )

            if not markText:
                continue

            for start in range(
                0,
                len(markText),
                2000,
            ):
                children.append(
                    get_callout(
                        markText[
                            start:start + 2000
                        ],
                        icon=BOOKMARK_CALLOUT_ICON,
                    )
                )

            abstract = data.get(
                "abstract"
            )

            if abstract:
                children.append(
                    get_quote(abstract)
                )

    valid_summary = [
        item
        for item in (summary or [])
        if (
            (item.get("review") or {}).get("content")
            or ""
        ).strip()
    ]

    if valid_summary:
        children.append(
            get_heading(
                1,
                "点评",
            )
        )

        for item in valid_summary:
            content = (
                item.get("review") or {}
            ).get("content") or ""

            if not content:
                continue

            for start in range(
                0,
                len(content),
                2000,
            ):
                children.append(
                    get_callout(
                        content[
                            start:start + 2000
                        ],
                        icon=NOTE_CALLOUT_ICON,
                    )
                )

    return children


# =========================================================
# 微信读书 Book ID
# =========================================================

def transform_id(book_id):
    id_length = len(book_id)

    if re.match(
        r"^\d*$",
        book_id,
    ):
        ary = []

        for i in range(
            0,
            id_length,
            9,
        ):
            ary.append(
                format(
                    int(
                        book_id[
                            i:min(
                                i + 9,
                                id_length,
                            )
                        ]
                    ),
                    "x",
                )
            )

        return "3", ary

    result = ""

    for i in range(id_length):
        result += format(
            ord(book_id[i]),
            "x",
        )

    return "4", [result]


def calculate_book_str_id(book_id):
    md5 = hashlib.md5()

    md5.update(
        book_id.encode("utf-8")
    )

    digest = md5.hexdigest()

    result = digest[0:3]

    code, transformed_ids = (
        transform_id(book_id)
    )

    result += (
        code
        + "2"
        + digest[-2:]
    )

    for i in range(
        len(transformed_ids)
    ):
        hex_length_str = format(
            len(transformed_ids[i]),
            "x",
        )

        if len(hex_length_str) == 1:
            hex_length_str = (
                "0"
                + hex_length_str
            )

        result += (
            hex_length_str
            + transformed_ids[i]
        )

        if (
            i
            < len(transformed_ids) - 1
        ):
            result += "g"

    if len(result) < 20:
        result += digest[
            0:20 - len(result)
        ]

    md5 = hashlib.md5()

    md5.update(
        result.encode("utf-8")
    )

    result += md5.hexdigest()[0:3]

    return result


# =========================================================
# 主同步流程
# =========================================================

def sync():
    global client
    global data_source_id
    global weread

    secrets = validate_secret_inputs()
    notion_id = extract_notion_id()

    notion_token = secrets["notion_token"]

    weread = WeReadGatewayClient(
        secrets["weread_api_key"]
    )

    client = Client(
        auth=notion_token,
        log_level=logging.ERROR,
        notion_version=NOTION_VERSION,
    )

    data_source_id = resolve_data_source_id(notion_id)

    print(f"Notion API Version: {NOTION_VERSION}")
    print(f"Notion Data Source ID: {data_source_id}")

    load_data_source_schema()
    ensure_year_property()

    books = get_notebooklist()

    if not books:
        print("微信读书没有获取到书籍")
        return

    print(f"共获取 {len(books)} 本书")

    success_count = 0
    error_count = 0
    new_book_count = 0
    updated_book_count = 0
    new_content_count = 0

    for index, item in enumerate(books):
        try:
            sort = item.get("sort") or 0
            book = item.get("book") or item

            title = book.get("title") or ""
            cover = (
                book.get("cover") or ""
            ).replace("/s_", "/t7_")

            book_id = book.get("bookId")
            author = book.get("author") or ""

            if not book_id:
                print(
                    f"[{index + 1}/{len(books)}] "
                    f"跳过没有 BookId 的书：{title}"
                )
                continue

            categories = book.get("categories")
            if categories is not None:
                categories = [
                    x.get("title")
                    for x in categories
                    if x.get("title")
                ]

            print(
                f"[{index + 1}/{len(books)}] "
                f"正在同步：《{title}》"
            )

            # =====================================================
            # A. 永远先同步书籍属性
            #
            # 这里故意不判断：
            #   - 有没有划线
            #   - 有没有笔记
            #   - 有没有读完
            #   - Sort 有没有变化
            #
            # 因为阅读进度可能变化，而 Sort 不一定变化。
            # =====================================================

            existing_page = find_existing_book(book_id)

            if has_any_property(("ISBN", "评分", "年份")):
                isbn, rating, year = get_bookinfo(book_id)
            else:
                isbn, rating, year = "", None, None

            read_info = None
            if has_any_property(
                ("状态", "阅读时长", "阅读进度", "时间")
            ):
                read_info = get_read_info(book_id)

                print(
                    f"    → 阅读信息："
                    f"{round(read_info.get('readingProgress', 0) * 100)}%，"
                    f"{format_reading_time(read_info.get('readingTime', 0))}"
                    + (
                        f"，日期 {read_info.get('readingDate')}"
                        if read_info.get("readingDate")
                        else ""
                    )
                )

            if existing_page is None:
                print(
                    f"    → Notion 不存在，创建书籍："
                    f"《{title}》"
                )

                page_id = insert_to_notion(
                    title,
                    book_id,
                    cover,
                    sort,
                    author,
                    isbn,
                    rating,
                    categories,
                    year=year,
                    read_info=read_info,
                )

                existing_keys = set()
                new_book_count += 1

            else:
                page_id = existing_page["id"]

                print(
                    f"    → Notion 已存在，"
                    f"更新书籍属性："
                    f"《{title}》"
                )

                update_existing_book_properties(
                    page_id,
                    title,
                    book_id,
                    cover,
                    sort,
                    author,
                    isbn,
                    rating,
                    categories,
                    year=year,
                    read_info=read_info,
                )

                existing_keys = get_existing_block_keys(
                    page_id
                )
                updated_book_count += 1

            # =====================================================
            # B. 书籍属性同步完成后，再处理划线/笔记
            #
            # 没有划线/笔记并不会阻止书籍属性更新。
            # =====================================================

            added = sync_book_content(
                page_id=page_id,
                book_id=book_id,
                title=title,
                existing_keys=existing_keys,
            )

            new_content_count += added
            success_count += 1

        except Exception as error:
            error_count += 1
            print(
                f"    ✗ 《{title or '未知书籍'}》同步失败："
                f"{type(error).__name__}: {error}"
            )
            # 一本书失败不能阻止后面的书继续同步。
            continue

    print("")
    print("========== 同步完成 ==========")
    print(f"成功：{success_count} 本")
    print(f"新建：{new_book_count} 本")
    print(f"更新：{updated_book_count} 本")
    print(f"新增内容块：{new_content_count}")
    print(f"失败：{error_count} 本")


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="weread2notion",
        description=(
            "Sync WeRead highlights "
            "and notes to Notion."
        ),
    )

    parser.add_argument(
        "command",
        nargs="?",
        default="sync",
        choices=["sync"],
        help=(
            "Command to run. "
            "Defaults to sync."
        ),
    )

    parser.parse_args(argv)

    try:
        sync()
    except ConfigError:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
