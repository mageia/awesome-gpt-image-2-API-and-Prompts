# HAND OFF

更新时间：2026-07-16（Asia/Shanghai）

## 任务背景

该仓库主要从互联网（以 X/Twitter 为主）收集 GPT-Image-2 prompt 及对应输出图片。原始抓取脚本没有进入 Git 历史，已经无法直接恢复。本轮工作根据以下残留产物反推并重建了抓取与初步策展流水线：

- `data/ingested_tweets.json`
- `data/curation_report_2026-06-02.json`
- `data/curation_report_2026-06-07.json`
- `data/curation_report_2026-06-08.json`
- `result/gpt_image_2_recent_prompts_*_summary.json`
- Git 历史中的 `data/valid_mapping_2026-05-08.json`
- Git 历史中的 `data/valid_mapping_2026-05-09.json`
- 各批次新增案例、图片及 Markdown 的提交差异

从残留数据可以确认，原流程大致为：

1. 用 10-13 个 GPT-Image-2 关键词搜索最近 24 小时推文。
2. 按 tweet ID 和已有 ingestion index 去重。
3. 排除无媒体、无完整内联 prompt 的帖子。
4. 从主帖、作者自回复或媒体描述中恢复 prompt。
5. 生成标题、分类、版式和 keep/drop 原因。
6. 人工批准后分配 Case 编号、下载图片并更新多语言 Markdown。

## 已实现内容

### 抓取 CLI

新增：`script/crawl_x_prompts.py`

主要能力：

- 纯 Python 标准库，无需安装第三方依赖。
- 支持 X API v2 recent search。
- 支持外部命令/本地 X CLI，只要命令输出 JSON。
- 支持本地 JSON、X API fixture 和历史 curation report 离线回放。
- 默认使用 10 个从历史报告恢复出的搜索词。
- 默认搜索最近 24 小时，自动避开 X 搜索索引的实时延迟。
- 合并多个搜索词返回的重复 tweet。
- 同时检查 `data/ingested_tweets.json`、`README*.md` 和 `cases/*.md`，防止重复入库。
- 可选补抓作者在同一 conversation 中的自回复。
- JSON 离线回放会先按 conversation ID 和作者过滤线程结果再应用数量上限，媒体合并前也会重复校验，避免候选继承无关推文图片。
- 从 `Prompt:`、独立 `Prompt` 行、结构化正文和媒体 alt-text 中提取 prompt。
- 保留 likes、views、retweets、replies、quotes 和媒体 URL。
- 启发式建议标题、7 类分类、Shape A/B 和 keep/review/drop。
- 支持 OpenAI-compatible chat-completions 接口进行语义评审。
- 下载输出媒体到临时审核目录，单文件上限 25 MiB。
- 原子写入 JSON，避免任务中断留下半个报告。
- 默认不修改 README 或正式案例目录。

支持的数据源：

- `--provider x-api`
- `--provider json`
- `--provider command`

### 文档和测试

新增：

- `docs/crawler.md`
- `tests/test_crawl_x_prompts.py`
- `tests/fixtures/x_api_search.json`
- `tests/fixtures/queries.txt`

修改：

- `.gitignore`
  - `tmp/` 继续忽略，用于默认抓取产物。
  - 不再忽略整个 `script/`、`data/`、`result/`，使新脚本和显式发布的报告可以正常进入 Git。

## 运行方法

### X API 在线抓取

必需环境变量：

```bash
export X_BEARER_TOKEN="..."
```

Token 对应的 X Developer App 必须具有 API v2 recent search 权限。

执行：

```bash
python3 script/crawl_x_prompts.py \
  --provider x-api \
  --window-hours 24 \
  --per-query-limit 100 \
  --enrich-threads
```

默认产物目录：

```text
tmp/awesome-gpt-image-2-prompts-daily-<UTC timestamp>/
```

### 使用外部 X CLI

通过命令模板接入 `bird` 或其他本地工具：

```bash
export X_SEARCH_COMMAND='bird search --json -n {limit} {query}'
python3 script/crawl_x_prompts.py --provider command
```

可用占位符：

- `{query}`
- `{start}`
- `{end}`
- `{limit}`

### 离线验证

```bash
python3 script/crawl_x_prompts.py \
  --provider json \
  --input-json tests/fixtures/x_api_search.json \
  --queries-file tests/fixtures/queries.txt \
  --end 2026-07-15T02:00:00Z \
  --download-media none \
  --output-dir tmp/crawler-fixture
```

### 启用模型语义评审

配置：

```bash
export CURATION_BASE_URL="https://your-provider.example/v1"
export CURATION_API_KEY="..."
export CURATION_MODEL="your-model"
```

执行：

```bash
python3 script/crawl_x_prompts.py \
  --provider x-api \
  --reviewer openai
```

模型必须返回包含以下字段的 JSON：

- `action`: `keep`、`review` 或 `drop`
- `title`
- `category`
- `reason`

## 产物契约

每次运行会生成：

- `candidate_tweets.json`：去重后的全部新候选。
- `prefiltered_candidates.json`：同时具有完整 prompt 和媒体的候选。
- `review_queue.json`：keep/review 候选。
- `search_meta.json`：查询词、查询返回 ID 和时间窗。
- `curation_report.json`：完整策展报告。
- `summary.json`：适合 cron/GitHub Actions 展示的摘要。
- `media/<tweet_id>/`：下载的输出图片。

显式添加 `--publish-report` 后，还会写入：

- `data/curation_report_<date>.json`
- `result/gpt_image_2_recent_prompts_<timestamp>_summary.json`

注意：`--publish-report` 仍不会更新 README、Case 编号或 `data/ingested_tweets.json`。

## GitHub Actions 部署方案

可以部署，无需常驻服务器。推荐使用定时 GitHub Actions：

- cron：`15 2 * * *`，约为北京时间每天 10:15。
- `actions/checkout@v4`
- `actions/setup-python@v5`，Python 3.12。
- 运行抓取脚本并把 `tmp/github-actions-${GITHUB_RUN_ID}` 上传为 Artifact。
- `actions/upload-artifact@v4`，建议保留 14 天。
- 使用 `concurrency` 防止同一天任务重叠。
- Artifact-only 模式只需要 `permissions: contents: read`。

GitHub Actions 必需 Secret：

- `X_BEARER_TOKEN`

可选语义评审配置：

- Secret：`CURATION_API_KEY`
- Variable：`CURATION_BASE_URL`
- Variable：`CURATION_MODEL`

当前尚未创建 `.github/workflows/daily-x-prompt-crawl.yml`。此前只提供了建议 YAML，没有写入仓库。

推荐先采用 Artifact-only 模式，不要自动提交或修改 README。原因是抓取内容仍需要版权、重复度和质量审核。

## 验证结果

已执行：

```bash
python3 -m unittest -v tests/test_crawl_x_prompts.py
python3 -m py_compile script/crawl_x_prompts.py tests/test_crawl_x_prompts.py
git diff --check
```

结果：

- 9 个单元测试全部通过。
- X API v2 fixture 端到端回放通过。
- 主帖 prompt 提取通过。
- 作者自回复 prompt 补全通过。
- 独立 `Prompt` 行解析通过。
- 媒体 alt-text prompt 恢复通过。
- ingestion index 去重通过。
- 历史 curation report 兼容解析通过。
- 发布 report/summary 到仓库外临时目录通过。
- 缺少 `X_BEARER_TOKEN` 时会明确退出并报错。
- 对 2026-06-02 的 51 条历史候选回放时，原来最终保留的 9 条仍被启发式识别为 keep。

未执行真实 X 在线抓取，因为当前环境没有 `X_BEARER_TOKEN`。

## 已知差异和限制

1. 这是根据产物重建的兼容实现，不是原脚本逐字恢复。
2. 新 prefilter 比历史流程更严格：只有恢复出完整 prompt 且存在媒体的候选才进入正式 prefilter。历史流程会把一部分只有 teaser 的帖子也交给人工审核。
3. 启发式分类和标题只适合作为初稿；大规模运行建议启用模型评审或人工复核。
4. 当前没有自动分配 Case 编号、复制媒体到 `images/<category>_caseN/`、更新 Markdown 或更新 ingestion index。
5. 当前没有 GitHub Actions workflow 文件，尚未真正部署定时任务。
6. X API 权限、价格和速率限制由实际 Developer App 套餐决定。
7. 外部命令 provider 使用本地受信任命令模板，不应接受不可信用户输入。
8. 第三方 prompt 和图片可能存在授权问题，不建议抓取后未经审核直接公开提交。

## 仓库现有问题（本轮未修复）

后续自动入库前应先处理：

- 英文 `README.md` 中 Comparison Case 108 被插入 Case 102 的代码围栏内部。
- README 和多个分类文件接近或超过 GitHub Markdown 渲染体积限制。
- 多语言完整分类页缺少约 85-93 个案例，并存在部分重复 Case ID。
- `script/sync_multilingual_readmes.py` 仍依赖旧版 README 固定文案，当前无法正常运行。
- 没有 CI 校验 Markdown 围栏、图片路径、计数、重复 ID 和多语言集合一致性。

## 后续建议顺序

1. 配置一个具有 recent search 权限的 `X_BEARER_TOKEN`，执行一次真实 24 小时抓取并检查响应字段和速率限制。
2. 将 Artifact-only GitHub Actions workflow 写入仓库并手工触发验证。
3. 修复 README Case 108、文件体积和多语言同步问题。
4. 新增“批准候选入库”脚本，输入人工确认的 JSON，输出 Case 编号、媒体目录、Markdown 和 ingestion index 更新。
5. 让入库脚本创建独立分支或 PR，不直接推送 `main`。
6. 添加 CI，至少验证代码围栏、图片存在性、Case ID、计数和多语言案例集合。

## 当前工作区状态

本轮修改尚未提交。预期 `git status --short` 包含：

```text
 M .gitignore
?? HANDOFF.md
?? docs/
?? script/crawl_x_prompts.py
?? tests/
```

未创建 `SERVER_INFO.md`，因为本轮没有实际部署服务或 GitHub Actions workflow。
