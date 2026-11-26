# 在 cron-job.org 建定时任务的步骤

目标：每 15 分钟自动调用一次 GitHub API，触发 `fetch.yml`，让云端抓取任务跑起来。

> 前提：Fine-grained PAT 已申请好，并已写入本地 `.cron.env`。下面的步骤里需要复制这个 token，但**不要把 token 写进 GitHub 仓库、不要发给我或别人**。

---

## 一、登录 cron-job.org

- [ ] 打开 https://cron-job.org/
- [ ] 注册 / 登录账号
- [ ] 进入控制台后，点 **Create cronjob**（新建定时任务）

---

## 二、填写基础信息

| 字段 | 填写内容 |
|---|---|
| Title / Name | `binance-trader-watch` |
| URL | `https://api.github.com/repos/davie-boop/binance-trader-watch/actions/workflows/fetch.yml/dispatches` |
| Request method | **POST** |
| Schedule | **Every 15 minutes** |
| Time zone | `UTC` 或 `Asia/Shanghai` 都可以（15 分钟间隔等价） |

如果 Schedule 里没有 "Every 15 minutes"，选 **Custom / Cron expression**，填写：

```cron
*/15 * * * *
```

> 注意：URL 里的 `fetch.yml` 是 workflow 文件名，不是数字 ID；不要改成 `/actions/runs` 或其他地址。

---

## 三、填写 Headers

在 **Headers / Custom headers / Advanced** 区域点 **Add header**，逐条添加下面 4 行：

| Header | Value |
|---|---|
| `Accept` | `application/vnd.github+json` |
| `Authorization` | `Bearer <粘贴 .cron.env 里 TW_GITHUB_TOKEN= 后面的值>` |
| `X-GitHub-Api-Version` | `2022-11-28` |
| `Content-Type` | `application/json` |

### Authorization 怎么填

1. 打开本地文件：

   `C:\Users\pingf\WorkBuddy\2026-11-26-09-19-29\binance-trader-watcher\.cron.env`

2. 复制 `TW_GITHUB_TOKEN=` 后面的 `github_pat_...` 那一整串。
3. 在 cron-job.org 的 `Authorization` 里填成：

```text
Bearer github_pat_你复制的那串token
```

要点：

- `Bearer` 后面有**一个空格**
- token 前后不要加引号
- 不要把 token 填到 URL 里
- 不要把 token 写进 `CRON兜底配置.md` 或任何会提交到 GitHub 的文件

---

## 四、填写 Body

在 **Request body / POST data / Body** 区域填入：

```json
{"ref":"main"}
```

注意：

- 必须是 JSON，只有 `ref` 一个字段
- 分支写 `main`
- 不要加换行、注释或其他字段
- `Content-Type: application/json` 已经在 Headers 里配置

---

## 五、保存并立即测试

- [ ] 点 **Save** / **Create cronjob** 保存
- [ ] 在任务列表里点 **Run now** / **Execute now** 手动执行一次
- [ ] 观察执行结果：HTTP **204** 即为成功
- [ ] 打开 Actions 页面确认新运行：  
  https://github.com/davie-boop/binance-trader-watch/actions

应出现一条事件为 `workflow_dispatch` 的新运行，状态最终为 **success**。

---

## 六、确认自动定时生效

保存成功并手动测试通过后：

- 等下一个 15 分钟窗口（例如 16:00、16:15、16:30、16:45）
- 回 cron-job.org 的 **Execution history / History** 看是否自动出现一条 204 记录
- 再去 GitHub Actions 页面看是否自动多出一条 `workflow_dispatch` 运行

两条都出现，说明外部定时已正式接管。

---

## 七、常见报错

| 状态码 / 现象 | 原因 | 处理 |
|---|---|---|
| 401 | token 无效、过期，或漏了 `Bearer ` 前缀 | 检查 Authorization 格式；必要时重新生成 PAT |
| 403 | Fine-grained PAT 权限不足或仓库没选对 | 确认 `Actions: Read and write`、`Contents: Read-only`，仓库只选 `binance-trader-watch` |
| 404 | URL 写错、owner/repo 写错 | 对照上面 URL 逐字检查 |
| 405 | Method 选成了 GET | 改成 **POST** |
| 422 | Body 不对或分支不存在 | Body 必须是 `{"ref":"main"}` |
| 204 但 Actions 没立刻出现 | GitHub 排队有几十秒延迟 | 等 30-60 秒刷新 Actions 页面 |
| 两条运行同时出现 | GitHub 自带 schedule 也恢复了 | `cloud_state.json` 会去重，不会重复推微信；稳定后可注释掉 `fetch.yml` 里的 schedule |

---

## 八、保存后建议勾选

如果 cron-job.org 有这些选项，建议开启：

- **Notify on failure / 失败通知**：请求非 2xx 时发邮件提醒
- **Save responses / 保存响应历史**：方便回看每次状态码
- **Timeout**：保持默认即可，或设 30 秒

---

## 九、安全与到期提醒

- `.cron.env` 已被 `.gitignore` 忽略，不会被同步到 GitHub。
- Fine-grained PAT 建议 90 天过期；到期前 7 天重新生成一个，更新 cron-job.org 的 `Authorization` Header，再点一次 **Run now** 验证。
- 如果怀疑 token 泄露，立刻到 https://github.com/settings/personal-access-tokens 点 **Revoke**，再换新 token。
- 不要把 cron-job.org 的任务截图（含 Authorization Header）发给别人。
