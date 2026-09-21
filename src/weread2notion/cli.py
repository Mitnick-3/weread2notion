# ========== 新增：根据BookId查询Notion页面，返回page_id或None ==========
def find_book_page_by_bookid(bookId):
    filter = build_equals_filter("BookId", bookId)
    response = query_data_source(filter=filter)
    results = response.get("results", [])
    if len(results) > 0:
        return results[0]["id"]
    return None

# ========== 新增：更新已存在页面的属性 ==========
@retry(stop_max_attempt_number=3, wait_fixed=5000)
def update_notion_page_properties(page_id, properties):
    client.pages.update(
        page_id=page_id,
        properties=properties
    )

# ========== 新增：清空页面原有子块（划线笔记，保留页面本身） ==========
@retry(stop_max_attempt_number=3, wait_fixed=5000)
def clear_page_children(page_id):
    block_resp = client.blocks.children.list(block_id=page_id)
    blocks = block_resp.get("results", [])
    for b in blocks:
        try:
            client.blocks.delete(block_id=b["id"])
            time.sleep(0.15)
        except Exception as e:
            print(f"删除旧块警告 {b['id']}: {e}")

# ========== 修改 insert_to_notion，改为UPSERT（存在更新，不存在新建） ==========
def insert_to_notion(bookName, bookId, cover, sort, author, isbn, rating, categories):
    """
    Upsert：存在则更新属性，不存在新建页面
    返回 page_id
    """
    if not cover or not cover.startswith("http"):
        cover = "https://www.notion.so/icons/book_gray.svg"
    parent = {"type": "data_source_id", "data_source_id": data_source_id}
    raw_properties = {
        title_property_name: bookName,
        "BookId": bookId,
        "ISBN": isbn,
        "链接": f"https://weread.qq.com/web/reader/{calculate_book_str_id(bookId)}",
        "作者": author,
        "Sort": sort,
        "评分": rating,
    }
    if categories != None:
        raw_properties["分类"] = categories

    read_info = (
        get_read_info(bookId=bookId)
        if has_any_property(("状态", "阅读时长", "阅读进度", "时间"))
        else None
    )
    if read_info != None:
        markedStatus = read_info.get("markedStatus", 0)
        readingTime = read_info.get("readingTime", 0)
        readingProgress = read_info.get("readingProgress", 0)
        format_time = ""
        hour = readingTime // 3600
        if hour > 0:
            format_time += f"{hour}时"
        minutes = readingTime % 3600 // 60
        if minutes > 0:
            format_time += f"{minutes}分"
        raw_properties["状态"] = "读完" if markedStatus == 4 else "在读"
        raw_properties["阅读时长"] = format_time
        raw_properties["阅读时长(秒)"] = readingTime  # 新增：原始秒数，数字字段
        raw_properties["阅读进度"] = readingProgress
        if "finishedDate" in read_info:
            raw_properties["时间"] = datetime.utcfromtimestamp(
                read_info.get("finishedDate")
            ).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
    properties = build_notion_properties(raw_properties)
    icon = get_icon(cover)

    # UPSERT 核心逻辑
    exist_page_id = find_book_page_by_bookid(bookId)
    if exist_page_id:
        # 已有页面：只更新属性，不重建页面
        update_notion_page_properties(exist_page_id, properties)
        page_id = exist_page_id
        print(f"✅ 书籍已存在，更新属性 page_id={page_id}")
    else:
        # 新书籍：新建页面
        response = client.pages.create(parent=parent, icon=icon, cover=icon, properties=properties)
        page_id = response["id"]
        print(f"🆕 新建书籍页面 page_id={page_id}")
    return page_id


# ========== 移除旧的 check() 函数，删除这一段全部 ==========
# def check(bookId):
#     """检查是否已经插入过 如果已经插入了就删除"""
#     filter = build_equals_filter("BookId", bookId)
#     response = query_data_source(filter=filter)
#     for result in response["results"]:
#         try:
#             client.blocks.delete(block_id=result["id"])
#         except Exception as e:
#             print(f"删除块时出错: {e}")

# ========== 修改 sync() 主循环，【二选一模式】==========
def sync():
    global client, data_source_id, weread
    secrets = validate_secret_inputs()
    notion_id = extract_notion_id()
    notion_token = secrets["notion_token"]
    weread = WeReadGatewayClient(secrets["weread_api_key"])
    client = Client(
        auth=notion_token,
        log_level=logging.ERROR,
        notion_version=NOTION_VERSION,
    )
    data_source_id = resolve_data_source_id(notion_id)
    print(f"Notion API Version: {NOTION_VERSION}")
    print(f"Notion Data Source ID: {data_source_id}")
    load_data_source_schema()
    latest_sort = get_sort()
    books = get_notebooklist()
    if books != None:
        for index, book in enumerate(books):
            sort = book["sort"]

            # ====================== 模式开关，二选一 ======================
            # 模式1【推荐：增量更新，只有微信读书发生变更的书才同步，无划线也入库】
            if sort <= latest_sort:
                continue
            # 模式2【全量同步：每次运行同步书架全部书籍，不管sort，无划线也入库，注释上面一行，打开下面注释】
            # if False:
            # ===========================================================

            book = book.get("book") or book
            title = book.get("title") or ""
            cover = (book.get("cover") or "").replace("/s_", "/t7_")
            bookId = book.get("bookId")
            author = book.get("author") or ""
            if not bookId:
                continue
            categories = book.get("categories")
            if categories != None:
                categories = [x["title"] for x in categories]
            print(f"正在同步【{title}】({index+1}/{len(books)})")

            # ===== 删除旧 check(bookId) 调用 =====
            # check(bookId)

            if has_any_property(("ISBN", "评分")):
                isbn, rating = get_bookinfo(bookId)
            else:
                isbn, rating = "", None
            id = insert_to_notion(
                title, bookId, cover, sort, author, isbn, rating, categories
            )
            # ========== 新增：每次同步前清空旧划线笔记块 ==========
            clear_page_children(id)

            chapter = get_chapter_info(bookId)
            bookmark_list = get_bookmark_list(bookId)
            summary, reviews = get_review_list(bookId)
            bookmark_list.extend(reviews)
            bookmark_list = sorted(
                bookmark_list,
                key=lambda x: get_note_sort_key(x, chapter),
            )
            children, grandchild = get_children(chapter, summary, bookmark_list)
            # 哪怕children是空列表（没有划线笔记），add_children也安全执行，不会报错
            results = add_children(id, children)
            if len(grandchild) > 0 and results != None:
                add_grandchild(grandchild, results)
