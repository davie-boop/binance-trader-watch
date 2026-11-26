# 币安跟单交易员仓位最新交易监控

## 当前配置：只抓熬鹰资本（每 15 分钟自动）

`.env` 中 `TW_TRADER_IDS=5075281354358777856`（熬鹰资本）。所有命令不传 `--trader-id` 时都只针对这一个交易员。熬鹰资本隐藏了实时持仓，但**历史订单和平仓记录照常可抓**。

> **想让电脑断电关机也照常抓取并推微信？** 用 GitHub Actions 云端方案，完整步骤见 [`CLOUD_DEPLOY.md`](CLOUD_DEPLOY.md)。云端用 `cloud_state.json` 跨运行去重，本地方案则继续用 `one_click.bat`。

### 一键脚本（唯一入口）

项目根目录只有一个脚本 **`one_click.bat`**，双击它就能用，不用敲命令、不用管 Python 路径和编码：

1. **自动安装** Windows 定时任务（每 15 分钟抓一次最近 3 条）
2. **立即抓取**一次，当场看到结果
3. 完成后出现菜单，可继续选择：
   - `[1]` 实时看板（每 10 秒刷新）
   - `[2]` 查看任务状态和抓取日志
   - `[3]` 卸载定时任务
   - `[4]` 测试微信推送
   - `[0]` 退出

定时任务通过回调 `one_click.bat --fetch` 静默执行，日志写到 `logs\schedule_fetch.log`。关掉 WorkBuddy、重启电脑后依然每 15 分钟自动运行。

### 微信推送（可选）

每抓取一次就把最新 3 条订单/平仓推到微信。在 `.env` 中配置：

1. **选渠道**：`TW_NOTIFY_CHANNEL=pushplus`（推荐，实名用户 200 条/天，够每 15 分钟一次）。
   - `serverchan`：Server 酱，免费版每天仅 5 条，不够；要 39 元/年。
   - `wecom`：企业微信群机器人，需建群并添加机器人，推到群而非个人微信。
2. **填凭据**（以 PushPlus 为例）：微信扫码登录 https://www.pushplus.plus/ → 完成实名认证 → 在「一对一推送」页复制 token → 填入 `TW_PUSHPLUS_TOKEN`。
3. **推送频率**：`TW_NOTIFY_ONLY_NEW=false`（默认）每次抓取都推；改成 `true` 则只在有新记录时推，更省额度、不刷屏。
4. **自测**：双击 `one_click.bat` 选 `[4]`，看到「推送结果：ok」且微信收到测试消息即配置成功。

配置好后定时任务自动带推送，无需改脚本。`--fetch` 日志里会记录每次推送结果。

> 脚本是纯英文 ASCII + CRLF 编码。早期用中文名+括号时，Windows cmd 默认按 GBK 解析导致乱码、`schtasks` 命令被拆碎而安装失败——已避免。如提示权限不足，右键脚本选"以管理员身份运行"。

命令行手动抓取最近 3 条（等价于一键脚本的抓取动作）：

```bash
python -m trader_watcher.cli trades --limit 3
```

> 脚本内部固定使用 WorkBuddy 内置 Python（`C:\Users\pingf\.workbuddy\binaries\python\versions\3.13.12\python.exe`），因为系统 PATH 中的 `python` 是微软商店占位符，直接敲会弹出商店。换机器时需要改脚本里的这一行路径。

## 先说清楚一个前提

**币安主站合约 API（`/fapi/v2/positionRisk`）只能查你自己 API Key 对应的账户仓位**，没有任何公开接口能直接查"任意交易员"的持仓。要追踪别人，只有三条路：

| 方案 | 数据来源 | 优点 | 缺点 |
|------|----------|------|------|
| 跟单公开数据聚合（本项目采用） | Kopyon 等聚合币安跟单页面 | 结构化、有免费额度、字段稳定 | 第三方服务，非币安官方 |
| 直接抓币安跟单页面 | Playwright / 内部接口 | 免费 | 接口未文档化，随时失效；有封 IP 风险 |
| 链上地址追踪 | 区块链浏览器 API | 公开透明 | 只能追踪链上仓位，不是交易所合约仓位 |

如果你的交易员不是币安**带单交易员（Lead Trader）**，那他没公开持仓可看，任何程序都拿不到——这点要特别留意。

## 数据新鲜度（重要）

币安跟单页面上的持仓约 **每 5 分钟刷新一次**。所以"实时"的上限就是 5 分钟级别，把轮询间隔设成 30~60 秒已经足够，再快也拿不到新数据。Kopyon 付费版提供 WebSocket 推送 `positionEvents`（开/增/减/平），延迟更低，但底层仍然受币安刷新频率限制。

## 安装

```bash
uv venv && uv pip install -e ".[dev]"
# 或
pip install -e ".[dev]"
```

复制 `.env.example` 为 `.env`，填入 API Key 和交易员 id。交易员 id 在跟单主页 URL 的 `portfolioId` 参数里。

## 运行

```bash
# 检查一次
trader-watch once

# 持续监控
trader-watch watch

# 查看历史事件
trader-watch events --trader-id 5231135285416047873
```

## 事件类型

| 事件 | 含义 |
|------|------|
| `opened` | 新开仓 |
| `increased` | 加仓 |
| `decreased` | 减仓（未清零） |
| `closed` | 平仓 |
| `hidden` | 交易员隐藏了持仓，**不是**平仓 |

程序每次拉取都会存一份快照，然后和上一份快照 diff 出事件，写入 SQLite。变动小于 0.01% 视为浮点抖动，不产生事件。

## 抓取逐笔交易记录（订单 + 平仓回合）

这是比仓位快照更细的数据，两种都存 SQLite，**自动增量去重**（重复运行不会产生重复行）：

```bash
# 同时抓订单和已平仓回合，最多翻 3 页（每页约 20 条）
trader-watch trades --trader-id 5231135285416047873 --kind all --pages 3

# 只抓订单 / 只抓平仓
trader-watch trades --trader-id 5231135285416047873 --kind orders
trader-watch trades --trader-id 5231135285416047873 --kind positions

# 查看已存记录
trader-watch order-list --trader-id 5231135285416047873 --limit 20
trader-watch position-list --trader-id 5231135285416047873 --limit 20
```

- `orders` 表：每笔委托，含时间、标的、买卖方向、价格、数量、订单类型、已实现盈亏。
- `closed_positions` 表：每次完整开平仓，含开仓价、平仓价、ROI、盈亏、杠杆、持仓时长。

翻页遇到"本页新增 0 条"会自动停止，所以日常定时执行同一条命令即可只补新数据。

**关于 API Key**：不配置 Key 时走每 IP 每天 10 次的匿名额度（足够测试）；要长期运行就在 `.env` 里配置免费的 `TW_KOPYON_API_KEY`（10,000 次/月）。注意 Key 和匿名额度分开计数。

### 实时看板（推荐：实时 + 只看最新 N 条 + 随时切换交易员）

```bash
# 启动：列出 .env 中所有交易员，可随时切换
python -m trader_watcher.cli live --limit 5 --interval 10

# 只盯某一个交易员，只看订单
python -m trader_watcher.cli live --trader-id 5318041645102466561 --kind orders
```

- 每轮只取**时间最新的 N 条**（默认 5，`--limit` 可调），首轮和切换交易员时显示全量，之后只打印**新增**的记录。
- 运行时按键：`1`~`6` 切换到第 N 个交易员（切换后立即显示他的最新 N 条）、`q` 退出、其他键下一轮继续轮询。
- 订单是逐笔产生的，不像持仓 5 分钟才刷新，所以 `--interval 10`（秒）已接近实时。
- 所有记录仍会落库去重，`live` 只是显示层；想查全部历史用 `order-list` / `position-list`。

## 换成其他数据源

实现 `sources/base.py` 里的 `PositionSource` 协议即可：

```python
class MySource:
    async def fetch_positions(self, trader_id: str) -> PositionSnapshot: ...
    async def aclose(self) -> None: ...
```

然后在 `cli.py` 的 `_build()` 里换掉 `KopyonSource`，业务逻辑完全不用改。

## 加通知

给 `TraderWatcher` 传一个异步回调：

```python
async def notify(event: PositionEvent) -> None:
    await send_to_telegram(event.model_dump_json())

watcher = TraderWatcher(source, store, on_event=notify)
```

## 踩坑提示

1. **交易员隐藏持仓**：返回 `hidden=true` + 空列表，程序会发 `HIDDEN` 事件而不是误判成全平。
2. **同一标的双向持仓**：用 `symbol:side` 作为唯一键，多空分开跟踪。
3. **API 限流**：免费版 10000 次/月、120 次/分钟；监控 10 个交易员、60 秒间隔约 43 万次/月，会超额度——要么减少交易员数量，要么放大间隔到 10 分钟以上，要么升级套餐。
4. **不要在代码里硬编码 API Key**：用 `.env`，并把它加进 `.gitignore`。
5. **直连币安网页的接口不稳定**：GitHub 上有项目用 `POST /bapi/futures/v1/public/future/copy-trade/lead-portfolio/trade-history` 抓交易记录，但该接口未文档化、国内网络通常不可达，且币安随时可能改动或封禁，因此本项目走聚合数据源。如果你本地有代理并坚持直连，可在 `sources/` 下新增一个实现相同方法的 `BinanceWebSource`。
6. **"组合已关闭"的交易员无法监控实时仓位**：`once` / `watch` 依赖 `openPositions` 接口。若该交易员的跟单组合已结束，接口返回 `HTTP 422: The current portfolio is closed.`，程序会打印 `跳过：...` 而不中断。**这类交易员的历史订单和平仓记录仍可用 `trades` 正常抓取**——配置交易员时请优先选仍在带单、有公开持仓的活跃组合。
7. **部分平仓记录没有 `closedAt`**：`positionHistory` 中 `status=PARTIALLY_CLOSED` 的记录只有 `updatedAt`，模型会自动用 `updatedAt` 兜底作为平仓时间，不会让整批数据解析失败。
8. **批量抓取**：`trades` 不带 `--trader-id` 时会抓取 `.env` 中 `TW_TRADER_IDS` 的全部交易员，单个失败只跳过该交易员，不影响其他人。
9. **时间显示**：API 返回的时间戳是 UTC，终端/`live` 看板显示时统一转换为北京时间（UTC+8），与币安网页一致；数据库里仍以 UTC 存储，避免时区转换影响去重键。

## 测试

```bash
pytest -q
```
