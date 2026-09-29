#!/usr/bin/env python3
"""
安全清理历史「本地下载盘」残留（Link Curation 纯血版）。

默认目标：data/vault/
- 删除大文件 / 解压目录 / harvest 断点
- 可选迁移轻量 JSON 到 data/
- 默认 dry-run；加 --apply 才真正删除

用法：
  python cleanup_legacy_vault.py           # 仅预览
  python cleanup_legacy_vault.py --apply   # 执行清理
  python cleanup_legacy_vault.py --apply --purge-incoming
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_VAULT = ROOT / "data" / "vault"
LIGHT_NAMES = ("search_rules.txt", "longtail_queries.json")


def _human(n: int) -> str:
    if n >= 1024**3:
        return f"{n / 1024**3:.2f} GB"
    if n >= 1024**2:
        return f"{n / 1024**2:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n} B"


def scan(vault: Path) -> dict:
    files: list[Path] = []
    total = 0
    if not vault.exists():
        return {"exists": False, "files": 0, "bytes": 0, "paths": []}
    for p in vault.rglob("*"):
        if p.is_file():
            files.append(p)
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return {
        "exists": True,
        "files": len(files),
        "bytes": total,
        "paths": files,
    }


def migrate_light(vault: Path, data_dir: Path, *, apply: bool) -> list[str]:
    moved: list[str] = []
    data_dir.mkdir(parents=True, exist_ok=True)
    for name in LIGHT_NAMES:
        src = vault / name
        dst = data_dir / name
        if not src.is_file():
            continue
        if dst.exists():
            moved.append(f"skip {name} (data/ already has it)")
            continue
        if apply:
            dst.write_bytes(src.read_bytes())
        moved.append(f"{'moved' if apply else 'would_move'} {name} -> data/{name}")
    return moved


def cleanup(
    vault: Path,
    *,
    apply: bool,
    purge_incoming: bool = True,
) -> dict:
    info = scan(vault)
    report: dict = {
        "vault": str(vault),
        "apply": apply,
        "before": {"files": info["files"], "bytes": info["bytes"], "size": _human(info["bytes"])},
        "migrations": [],
        "actions": [],
    }
    if not info["exists"]:
        report["actions"].append("nothing_to_do (data/vault missing)")
        return report

    report["migrations"] = migrate_light(vault, ROOT / "data", apply=apply)

    if not apply:
        report["actions"].append(
            f"dry-run: would remove entire {vault} ({info['files']} files, {_human(info['bytes'])})"
        )
        return report

    # 真正删除
    shutil.rmtree(vault, ignore_errors=True)
    report["actions"].append(f"removed {vault}")
    # 确保不再自动被误建成「下载盘」占位
    # （Link Curation 不需要该目录；索引在 vault_index.json）
    after = scan(vault)
    report["after"] = {
        "exists": after["exists"],
        "files": after["files"],
        "bytes": after["bytes"],
        "size": _human(after["bytes"]),
    }
    del purge_incoming  # 整树删除已覆盖 _incoming
    return report


def main() -> int:
    p = argparse.ArgumentParser(description="清理 Link Curation 遗留本地下载盘")
    p.add_argument("--vault", type=Path, default=DEFAULT_VAULT)
    p.add_argument("--apply", action="store_true", help="真正删除（默认仅预览）")
    p.add_argument(
        "--purge-incoming",
        action="store_true",
        default=True,
        help="一并清理 _incoming（默认开，整树删除时已包含）",
    )
    args = p.parse_args()
    report = cleanup(args.vault, apply=args.apply, purge_incoming=args.purge_incoming)
    print("=== cleanup_legacy_vault ===")
    print(f"vault: {report['vault']}")
    print(f"mode:  {'APPLY' if report['apply'] else 'DRY-RUN'}")
    b = report["before"]
    print(f"before: {b['files']} files / {b['size']}")
    for m in report["migrations"]:
        print(f"  migrate: {m}")
    for a in report["actions"]:
        print(f"  action:  {a}")
    if "after" in report:
        a = report["after"]
        print(f"after:  exists={a['exists']} files={a['files']} / {a['size']}")
    if not args.apply:
        print("\n预览结束。确认无误后执行: python cleanup_legacy_vault.py --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
