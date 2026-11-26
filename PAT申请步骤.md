# Fine-grained PAT 申请步骤（给外部 cron 用）

这个 token 只用于让 cron-job.org 每 15 分钟调用 GitHub API 触发抓取任务。**不要用你的 GitHub 登录密码，也不要把 token 发给我或提交到仓库。**

## 一、逐项点击清单

- [ ] **1. 打开 token 页面**  
  https://github.com/settings/personal-access-tokens  
  确认右上角登录账号是 `davie-boop`。

- [ ] **2. 新建 Fine-grained token**  
  点击 **Generate new token** → 选择 **Fine-grained token**。  
  不要选 Classic token（权限太大）。

- [ ] **3. 填写基本信息**  
  | 字段 | 填写 |
  |---|---|
  | Token name | `binance-trader-watch-cron` |
  | Description | `External cron for workflow_dispatch`（可留空） |
  | Resource owner | `davie-boop`（个人账号，不要选组织） |
  | Expiration | 选 **90 days**（到期再换，最稳妥） |

- [ ] **4. 限定仓库范围**  
  Repository access 选择 **Only select repositories**，然后只勾选：  
  `davie-boop/binance-trader-watch`  
  **不要选 All repositories。**

- [ ] **5. 设置权限**  
  进入 **Permissions → Repository permissions**，用搜索框找到并设置：

  | 权限 | 选择 |
  |---|---|
  | `Actions` | **Read and write** |
  | `Contents` | **Read-only** |

  其他权限保持默认 / No access 即可。`Metadata` 通常是自动只读，不用改。

- [ ] **6. 生成并复制 token**  
  拉到页面底部点 **Generate token**。  
  生成后立即点 **Copy token**，保存到一个临时安全位置。  
  **这个页面只显示一次，关掉后看不到；丢了只能重新生成。**

  Fine-grained token 通常以 `github_pat_` 开头。

## 二、把 token 放到哪里

### 方式 A：cron-job.org（推荐，电脑关机也能跑）

在 cron-job.org 的任务 Headers 里添加一行：

| Header | Value |
|---|---|
| `Authorization` | `Bearer github_pat_你复制的token` |

其他配置保持不变：

- Method：`POST`
- URL：`https://api.github.com/repos/davie-boop/binance-trader-watch/actions/workflows/fetch.yml/dispatches`
- Body：`{"ref":"main"}`

### 方式 B：本地脚本测试

在项目目录把 `.cron.env.example` 复制为 `.cron.env`，填入：

```ini
TW_GITHUB_TOKEN=github_pat_你复制的token
```

然后运行：

```bash
python cron_dispatch.py
```

看到 `触发成功：GitHub Actions 已收到抓取请求。` 就说明 token 可用。

`.cron.env` 已被 `.gitignore` 忽略，不会被提交到 GitHub。

## 三、验证是否真的生效

1. 在 cron-job.org 点 **Run now**，或在本地运行 `python cron_dispatch.py`。
2. 打开 Actions 页面：  
   https://github.com/davie-boop/binance-trader-watch/actions
3. 应出现一条事件为 `workflow_dispatch` 的新运行，状态最终为 **success**。
4. 如果 GitHub 自带 schedule 也恢复触发，同一时间可能有两条运行；`cloud_state.json` 会去重，不会重复推微信。

## 四、常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| 401 / 403 | token 过期、权限不足、复制不完整 | 检查 `Actions: Read and write`，重新复制或重新生成 |
| 404 | 仓库范围没选对、owner 选错 | 必须选 `davie-boop` + `binance-trader-watch` |
| 422 | body 或分支不对 | body 必须是 `{"ref":"main"}` |
| token 只看到一次 | GitHub 的安全设计 | 没存下来就重新 Generate 一个新 token |
| 怀疑泄露 | token 被别人拿到 | 立刻回 https://github.com/settings/personal-access-tokens 点 **Revoke**，再生成新的并更新 cron-job.org |

## 五、到期提醒

建议生成后顺手在手机日历加一个提醒：**到期前 7 天更换 PAT**。  
更换流程：生成新 token → 更新 cron-job.org 的 `Authorization` Header → 点一次 Run now 验证 → Revoke 旧 token。
