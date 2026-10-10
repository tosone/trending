# 任务：抓取 GitHub Trending，把还没收录的仓库补进 data/

你（pi）在 GitHub Actions 里运行，工作目录就是本仓库（newspaper 风格的 trending 静态站）。
先读一遍 `AGENTS.md`（数据规则与「不要做什么」），然后严格按下面的步骤执行，不要做额外改动。

## 参数

本次运行的语言、榜单窗口和数量上限已经放在环境变量里，直接原样使用，不要改写成别的值：

- `TRENDING_LANGUAGES`（默认 `all`）
- `TRENDING_SINCE`（`all` / `daily` / `weekly` / `monthly`）
- `TRENDING_LIMIT`（默认 `25`）

## 步骤

1. 跑一次完整抓取（会抓总榜 + 各语言榜，合并进 `data/*.json`，只增不删、按 star 降序）：

   ```bash
   python3 tools/fetch_trending.py --languages "$TRENDING_LANGUAGES" --since "$TRENDING_SINCE" --limit "$TRENDING_LIMIT"
   ```

   抓取依赖已登录的 `gh`（`GH_TOKEN` 已在环境里），生成中文简介依赖 `DEEPSEEK_API_KEY`。如果脚本报错，把关键报错贴出来并停止，不要自己编数据。

2. 校验数据：

   ```bash
   python3 tools/check.py
   ```

3. 用 git 找出**本次真正新增**的仓库（老仓库只会刷新 star / 更新时间，不算新增）：

   ```bash
   git diff HEAD -- data/ | grep '^+' | grep '"full_name"'
   ```

4. 逐个检查新增仓库的 `summary`：必须是「基于 README 的中文简介、≤300 字、不编造版本号 / 性能数字 / 许可证」。
   哪一个明显跑偏、过短或不是中文，就用定向模式重写它，然后重新跑一次 `tools/check.py`：

   ```bash
   python3 tools/fetch_trending.py --repos owner/repo --resummarize
   ```

5. 只在 `data/` 有变化时提交：

   - 有新增仓库：`git add data`，提交信息用 `data: add owner/repo, owner/repo`（只列新增的，最多列 3 个，其余写「等 N 个」）。
   - 没有新增、只有老仓库刷新：提交信息用 `data: refresh trending`。
   - 完全没有变化就什么都不要提交。

   **不要 `git push`**，也不要用 `--force`、`reset --hard`、修改 git config；推送由 workflow 负责。

6. 最后用一段中文汇报：本次有没有新仓库、各自的语言和 star、`tools/check.py` 是否通过、提交了什么（或为什么没提交）。

## 硬性要求

- 不要手工编辑 `data/*.json` 的结构，也不要手写 `data/all.json`、`data/other.json` 的内容 —— 它们只由脚本生成。
- 不要引入新依赖、构建步骤、后端、CDN 或远程字体。
- 简介只写 README 里的事实；不写许可证，不写「今日新增 star」。
- 只在需要新增仓库时改动 `data/`（脚本的运行产物除外）；不要动 `index.html`、`assets/`、`tools/`。
