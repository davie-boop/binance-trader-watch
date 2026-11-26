# GitHub Actions 定时兜底配置（cron-job.org）

GitHub 自带的 `schedule` 目前没有按点触发，但 `workflow_dispatch` 手动触发是正常的。因此用外部 cron 服务每 15 分钟调用一次 GitHub API，效果等同于手动点「Run workflow」，电脑关机也能运行。

## 1. 创建 GitHub Fine-grained PAT

1. 打开：https://github.com/settings/personal-access-tokens
2. 点击 **Generate new token → Fine-grained token**
3. Token name：`binance-trader-watch-cron`
4. Expiration：建议 90 天（到期需更换）
5. Resource owner：`davie-boop`
6. Repository access：**Only select repositories** → 勾选 `binance-trader-watch`
7. Permissions → Repository permissions：
   - `Actions`：**Read and write**
   - `Contents`：**Read-only**（保险项，用于读取默认分支）
8. 生成后复制 token（`github_pat_...`），**不要提交到仓库、不要发给别人**。

> 传统 PAT（classic）也可以，但只需勾选 `repo`（或 public_repo）即可，权限比细粒度 token 更大，优先用上面的细粒度 PAT。

## 2. cron-job.org 任务配置

打开 https://cron-job.org/ ，注册/登录后 **Create cronjob**：

| 字段 | 填写内容 |
|---|---|
| Title | `binance-trader-watch` |
| URL | `https://api.github.com/repos/davie-boop/binance-trader-watch/actions/workflows/fetch.yml/dispatches` |
| Method | `POST` |
| Schedule | Every 15 minutes（或自定义 `*/15 * * * *`） |
| Timezone | UTC 或 Asia/Shanghai 都可以（15 分钟间隔等价） |

### Headers（逐条添加）

| Header | Value |
|---|---|
| `Accept` | `application/vnd.github+json` |
| `Authorization` | `Bearer 你的github_pat_token` |
| `X-GitHub-Api-Version` | `2022-11-28` |
| `Content-Type` | `application/json` |

### Request body

```json
{"ref":"main"}
```

保存后，先点任务列表里的 **Run now** 测试一次：

- GitHub Actions 页面应出现一条事件为 `workflow_dispatch` 的新运行；
- 运行成功即代表兜底链路打通；
- 如果请求返回 204，cron-job.org 会显示成功（GitHub 这个接口成功时无响应体）。

## 3. 本地调用脚本（可选，方便手动/自建服务器 cron）

项目根目录已提供 `cron_dispatch.py`。

### dry-run（不发送请求）

```bash
python cron_dispatch.py --dry-run
```

### 真实触发

把 `.cron.env.example` 复制为 `.cron.env`，填入 token：

```ini
TW_GITHUB_TOKEN=github_pat_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

然后运行：

```bash
python cron_dispatch.py
```

`.cron.env` 已被 `.gitignore` 忽略，不会被提交。

### Linux / Mac crontab 示例

```cron
*/15 * * * * cd /path/to/binance-trader-watcher && /usr/bin/python3 cron_dispatch.py >> logs/cron_dispatch.log 2>&1
```

### curl 示例（无 Python 环境时）

```bash
curl -sS -X POST \
  -H "Accept: application/vnd.github+json" \
  -H "Authorization: Bearer $TW_GITHUB_TOKEN" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  -H "Content-Type: application/json" \
  -d '{"ref":"main"}' \
  https://api.github.com/repos/davie-boop/binance-trader-watch/actions/workflows/fetch.yml/dispatches
```

## 4. 启用外部 cron 后的注意事项

1. **避免双重触发**：目前 `fetch.yml` 里 GitHub 自带 `schedule` 仍保留。确认外部 cron 稳定运行后，建议删除或注释掉这段，只保留外部触发：

   ```yaml
   on:
     # schedule:
     #   - cron: '1,16,31,46 * * * *'
     workflow_dispatch:
   ```

   重复触发不会重复推送微信（`cloud_state.json` 会去重），但会多消耗 Actions 分钟数。

2. **Token 安全**：PAT 只给这个仓库、只给必要权限；如果怀疑泄露，立刻在 GitHub 设置里 Revoke 并轮换。
3. **到期提醒**：细粒度 PAT 有有效期，到期后 cron 会返回 401/403，需要生成新 token 并更新 cron-job.org 的 Header。
4. **排查入口**：GitHub Actions 页面 https://github.com/davie-boop/binance-trader-watch/actions ；cron-job.org 里也能看到每次请求的 HTTP 状态码。

## 5. 常见返回码

| 状态码 | 含义 | 处理 |
|---|---|---|
| 204 | 成功 | 正常，GitHub 已排队运行 |
| 401 / 403 | token 无效或权限不足 | 检查 PAT、Actions: write 权限、是否过期 |
| 404 | 仓库或 workflow 名不对 | 核对 URL 中的 owner/repo 和 `fetch.yml` |
| 422 | 请求格式或分支问题 | 确认 body 是 `{"ref":"main"}` 且默认分支存在 |
