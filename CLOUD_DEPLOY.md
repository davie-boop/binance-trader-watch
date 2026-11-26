# 云端部署指南（GitHub Actions）

把抓取搬到 GitHub 云端后，**本地电脑断电、关机都不影响**，每 15 分钟自动抓取并在有新交易时推送微信。

## 原理

- GitHub Actions 云端定时任务（`*/15 * * * *`）每 15 分钟运行一次。
- 每次运行是全新环境，本地 SQLite 不保留，所以用仓库里的 `cloud_state.json` 记录已推送过的记录 uid，实现跨运行去重。
- 有新订单/平仓才发微信，没有就静默；状态有变化时才提交回仓库。
- 密钥存在 GitHub Secrets（加密），不会出现在代码或公开日志里。

## 你需要做的（约 5 分钟）

### 第 1 步：在 GitHub 建一个仓库

1. 登录 https://github.com → 右上角 `+` → `New repository`
2. Repository name 填 `binance-trader-watch`（随意）
3. 选 **Public**（公有仓库 Actions 完全免费不限时长；代码里没有密钥，安全）
4. **不要**勾选 "Add a README file" 等任何初始化选项 → 点 `Create repository`
5. 创建后页面会显示一段 `git remote add origin ...` 的地址，形如：
   ```
   https://github.com/你的用户名/binance-trader-watch.git
   ```
   复制备用。

### 第 2 步：把项目推到 GitHub

在项目目录 `binance-trader-watcher` 下打开 Git Bash（或 PowerShell），依次执行：

```bash
cd "C:/Users/pingf/WorkBuddy/2026-11-26-09-19-29/binance-trader-watcher"
git init
git add .
git commit -m "initial commit"
git branch -M main
git remote add origin 你的仓库地址.git
git push -u origin main
```

> 如果 `git commit` 提示需要设置身份，先执行：
> ```bash
> git config user.name "你的名字"
> git config user.email "你的邮箱"
> ```
> 再重新 commit。

验证：刷新 GitHub 仓库页面，应能看到 `.github/workflows/fetch.yml`、`cloud_state.json`、源码等文件，**但不应有 `.env`**（含密钥，已被 .gitignore 排除）。

### 第 3 步：添加 Secrets（密钥）

在 GitHub 仓库页面：`Settings` → 左侧 `Secrets and variables` → `Actions` → `New repository secret`，逐一添加：

| Name | Value | 说明 |
|---|---|---|
| `TW_KOPYON_API_KEY` | 你的 Kopyon Key | 数据源密钥 |
| `TW_TRADER_IDS` | `5075281354358777856` | 熬鹰资本 |
| `TW_NOTIFY_CHANNEL` | `pushplus` | 通知渠道 |
| `TW_PUSHPLUS_TOKEN` | 你的 PushPlus 消息 token | 推微信用 |

添加后列表里应看到 4 条（值会显示为加密，看不到明文，正常）。

### 第 4 步：启用 Actions

1. 点仓库顶部 `Actions` 标签
2. 如果看到 "Workflows aren't being run..." 提示，点 `I understand my workflows, go ahead and enable them`
3. 左侧选 `Binance Trader Watch` → 右侧点 `Run workflow` → 再点绿色 `Run workflow` 手动跑一次
4. 等约 30 秒~1 分钟，刷新页面看运行结果：
   - 绿色对勾 = 成功，微信应收到一条新交易推送（若此刻有新交易）
   - 首次运行是初始化，不会推历史，日志会显示 `初始化=False 新订单=0`

### 第 5 步：确认定时生效

手动跑成功后，定时任务会自动每 15 分钟运行（UTC 时间 `*/15`，对应北京时间同样每 15 分钟，无时差问题）。在 Actions 页面能看到每次运行记录。

## 本地与云端的取舍

启用云端后，**建议卸载本地的 Windows 定时任务**，否则本地和云端各自独立去重，同一笔新交易可能收到两条重复微信：

- 双击 `one_click.bat` → 选 `[3]` 卸载定时任务

本地脚本仍保留，可用于临时手动抓取、看实时看板、测试推送（`[4]`）。

## 常见问题

- **Actions 显示红色失败**：点进去看日志。最常见是 Secrets 名拼错或没填，日志会提示缺哪个。
- **一直收不到微信**：先在 Actions 手动触发 `test` 或看 `cloud-fetch` 日志里的 `推送=` 字段；`ok` 表示服务端接受，没收到则是 PushPlus 侧问题（关注公众号、token 是否复制对）。
- **state 文件冲突**：《并发》已设为不允许同时运行，正常不会出现。
- **想改抓取间隔**：编辑 `.github/workflows/fetch.yml` 里的 cron，提交后生效。
- **想暂停云端**：Actions 页面禁用 workflow，或删掉 `fetch.yml`。

## 相关文件

- `.github/workflows/fetch.yml`：定时配置
- `src/trader_watcher/cloud.py`：云端无状态抓取逻辑
- `cloud_state.json`：跨运行去重状态（自动维护，别手动改）
