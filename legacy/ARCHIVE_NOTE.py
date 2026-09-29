"""
[已归档] 本地下载时代的 bulk_harvest 已移除重型实现。

请勿在此恢复 download_with_resume / extract_archive / run_harvest 落盘逻辑。
纯血版请使用：

  source_queue.py          # 白名单 ↔ sources 队列
  link_curator.py          # 验链 + vault_index / catalog
  commercial_pipeline.py   # 商业五阶段
  cleanup_legacy_vault.py  # 清理历史 data/vault

本目录仅作说明占位，避免双叙事。
"""
