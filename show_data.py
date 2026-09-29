"""在终端整洁打印：型号、来源网站、用途（人话版）。"""

from __future__ import annotations

from database import Database
from display_labels import enrich_forum, enrich_scraped


def _clip(text: object, width: int = 36) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= width else s[: width - 1] + "…"


def main() -> None:
    with Database() as db:
        scraped = [
            enrich_scraped(dict(r))
            for r in db._conn.execute(
                """
                SELECT id, title, device_model, status, value_score, source_url
                FROM scraped_items ORDER BY id DESC LIMIT 10
                """
            ).fetchall()
        ]
        forums = [
            enrich_forum(dict(r))
            for r in db._conn.execute(
                """
                SELECT id, title, device_model, device_category, status,
                       value_score, source, source_url, pain_point, error_codes
                FROM forum_posts ORDER BY id DESC LIMIT 10
                """
            ).fetchall()
        ]

    print("\n======== 线路A · 抓回来的页面/安装包（最新10条）========")
    print(
        f"{'型号':<22} {'类型':<16} {'来源网站':<28} {'用途':<36} 状态"
    )
    print("-" * 120)
    if not scraped:
        print("(暂无数据)")
    for r in scraped:
        print(
            f"{_clip(r['显示型号'],22):<22} "
            f"{_clip(r['资源类型'],16):<16} "
            f"{_clip(r['下载来源网站'],28):<28} "
            f"{_clip(r['设备用途说明'],36):<36} "
            f"{r.get('status')}"
        )

    print("\n======== 线路B · 论坛急需型号线索（最新10条）========")
    print(
        f"{'型号':<18} {'大类':<12} {'来源网站':<26} {'用途/痛点':<40} 状态"
    )
    print("-" * 120)
    if not forums:
        print("(暂无数据)")
    for r in forums:
        print(
            f"{_clip(r['显示型号'],18):<18} "
            f"{_clip(r['设备大类'],12):<12} "
            f"{_clip(r['下载来源网站'],26):<26} "
            f"{_clip(r['设备用途说明'],40):<40} "
            f"{r.get('status')}"
        )
    print()


if __name__ == "__main__":
    main()
