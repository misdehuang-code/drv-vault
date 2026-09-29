"""项目入口示例：初始化数据库并演示请求工具。"""

from __future__ import annotations

import logging

from database import Database, Status
from utils import fetch_text

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


def main() -> None:
    with Database() as db:
        item_id = db.insert_item(
            title="示例标题",
            device_model="Demo-X1",
            source_url="https://example.com/demo",
            raw_content=None,
            status=Status.PENDING,
        )
        print(f"已写入记录 id={item_id}")
        print(db.get_item(item_id))

    # 演示网络请求（可按需注释）
    html = fetch_text(
        "https://httpbin.org/html",
        max_retries=3,
        delay_range=(0.5, 1.0),
    )
    print(f"抓取到 HTML 长度: {len(html)}")


if __name__ == "__main__":
    main()
