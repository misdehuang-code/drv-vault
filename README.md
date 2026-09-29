# 驱匣 DrvVault · Link Curation 纯血版

全球稀缺工业/遗留硬件 **驱动与固件直链验证 + 英文 SEO 导航站**。  
**不本地下载、不解压、不囤积大文件。**

## 产品定位

| 做 | 不做 |
|----|------|
| 发现需求型号、挖真实直链（寻址） | **绝不**把安装包落到 `data/vault/` |
| HEAD 验活（必要时 ≤2KB Range 探针） | 不托管版权文件本体 |
| Ollama/规则生成海外 SEO 文案 | 不以「未知型号」充数 |
| 输出纯 JSON 导航索引 | 不以本地文件存在为上架门槛 |

`commercial_catalog.json` 核心字段：`model` / `category` / `description` / `original_direct_url`（`content_length` 仅来自 HTTP 头）。

## 商业主路径（唯一）

```text
DEMAND → ACQUIRE → FULFILL(验链) → INTENT(SEO) → MERCHANDIZE(索引/店面)
```

入口：

```bash
python admin_server.py          # http://127.0.0.1:8787 点「开始」
cd web && npm run dev           # http://127.0.0.1:3000 店面导航
python link_curator.py          # 单独跑验链+写索引
python sync_catalog.py          # 严格去重并双写 web/data/
python sync_catalog.py --deploy # 去重 + Cloudflare Pages（需 CLOUDFLARE_API_TOKEN）
```

管理台「Sync to Cloudflare」按钮：有未发布变更时变绿；点击后执行去重同步并调用 Wrangler。

## 核心模块

| 模块 | 职责 |
|------|------|
| `commercial_pipeline.py` | 五阶段编排 |
| `link_curator.py` | 验链 Agent → `vault_index.json` + catalog |
| `source_queue.py` | 白名单 ↔ `sources.txt` 队列 |
| `seeker_agent.py` / `github_miner.py` | 挖直链（只写 URL） |
| `longtail_demand.py` | 长尾检索词 + SEO 缓存到 `data/` |
| `bulk_harvest.py` | **已废弃下载**；仅兼容 `--sync-only` → `source_queue` |

## 关键产物（全是 JSON / 文本）

- `sources.txt` — 待验直链队列  
- `vault_index.json` — 已验链索引（`local_storage: false`）  
- `data/commercial_catalog.json` + `web/data/commercial_catalog.json` — 店面导航  
- `data/急需设备型号白名单.json` — 需求型号  
- `data/search_rules.txt` / `data/longtail_queries.json` — 检索与 SEO 缓存  

索引字段要点：`hardware_model`、`category`、`pain_point`、`verified_url`、`price`、`seo_title`…

## 清理历史本地下载盘

```bash
python cleanup_legacy_vault.py           # 预览
python cleanup_legacy_vault.py --apply   # 删除 data/vault/
```

## 环境

见 `requirements.txt`、`.env.example`（Ollama：`OLLAMA_BASE_URL` / `OLLAMA_MODEL`）。  
Ollama 不可达时自动降级规则文案，流水线不中断。
