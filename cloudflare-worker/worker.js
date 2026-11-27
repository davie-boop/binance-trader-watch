/**
 * Cloudflare Worker - 币安跟单交易记录定时触发器
 *
 * 作用：每 15 分钟调用 GitHub Actions 的 workflow_dispatch 接口，
 *       让云端一次性抓取全部交易员并汇总推送微信。
 *
 * 为什么用 Cloudflare Workers：
 *   - Cron Triggers 是平台原生定时器，不依赖第三方轮询服务
 *   - 海外节点直连 api.github.com，没有国内到 GitHub 的网络波动
 *   - 免费额度（每天 10 万次请求）远超本项目每天 96 次的需求
 *
 * 配置方式：
 *   - token 通过 `wrangler secret put TW_GITHUB_TOKEN` 设置，绝不写进代码
 *   - 交易员列表默认取下方常量；如需增删，用 `wrangler secret put TRADER_IDS`
 *     和 `wrangler secret put TRADER_LABELS` 覆盖，无需改代码
 */

// GitHub Actions 触发接口（成功时返回 204）
const DISPATCH_URL =
  "https://api.github.com/repos/davie-boop/binance-trader-watch/actions/workflows/fetch.yml/dispatches";

// 默认交易员 ID（逗号分隔）。三个「金木爱交易」是不同 ID，别名加后缀便于区分。
const DEFAULT_TRADER_IDS = [
  "5075281354358777856",
  "5261429206216458753",
  "5285947977434857729",
  "5305355976370339073",
  "5312928647505109504",
].join(",");

const DEFAULT_TRADER_LABELS = {
  "5075281354358777856": "熬鹰资本",
  "5261429206216458753": "爽翻翻",
  "5285947977434857729": "金木爱交易A",
  "5305355976370339073": "金木爱交易B",
  "5312928647505109504": "金木爱交易C",
};

/**
 * 触发一次云端抓取。
 *
 * @param {object} env Worker 环境变量（含 secret）
 * @returns {Promise<object>} 触发结果，便于日志与手动测试查看
 */
async function dispatch(env) {
  const token = env.TW_GITHUB_TOKEN;
  if (!token) {
    return { ok: false, status: 0, error: "未设置 TW_GITHUB_TOKEN，请执行 wrangler secret put TW_GITHUB_TOKEN" };
  }

  const traderIds = (env.TRADER_IDS || DEFAULT_TRADER_IDS).trim();
  let traderLabels = DEFAULT_TRADER_LABELS;
  if (env.TRADER_LABELS) {
    try {
      traderLabels = JSON.parse(env.TRADER_LABELS);
    } catch (err) {
      console.error("TRADER_LABELS 不是合法 JSON，使用默认别名：", err);
    }
  }

  const body = {
    ref: "main",
    inputs: {
      trader_ids: traderIds,
      trader_labels: JSON.stringify(traderLabels),
    },
  };

  const response = await fetch(DISPATCH_URL, {
    method: "POST",
    headers: {
      Accept: "application/vnd.github+json",
      Authorization: `Bearer ${token}`,
      "X-GitHub-Api-Version": "2022-11-28",
      "Content-Type": "application/json",
      "User-Agent": "binance-trader-watch-cf-worker/1.0",
    },
    body: JSON.stringify(body),
  });

  const text = await response.text();
  return {
    ok: response.status === 204,
    status: response.status,
    trader_count: traderIds.split(",").filter(Boolean).length,
    response: text.slice(0, 500),
    time: new Date().toISOString(),
  };
}

export default {
  /**
   * Cron Trigger 入口：由 wrangler.toml 的 crons 配置驱动。
   * 用 ctx.waitUntil 确保异步请求在 Worker 终止前完成。
   */
  async scheduled(controller, env, ctx) {
    console.log("Cron 触发时间：", controller.scheduledTime);
    ctx.waitUntil(
      dispatch(env).then((result) => {
        console.log("触发结果：", JSON.stringify(result));
      })
    );
  },

  /**
   * HTTP 入口：用于手动测试（浏览器访问 /trigger 即可触发一次）。
   * 正常运行时不会用到，仅方便验证 token 与网络是否通畅。
   */
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (url.pathname === "/trigger") {
      const result = await dispatch(env);
      return new Response(JSON.stringify(result, null, 2), {
        status: result.ok ? 200 : 502,
        headers: { "Content-Type": "application/json; charset=utf-8" },
      });
    }
    return new Response("OK - use /trigger to dispatch manually", {
      headers: { "Content-Type": "text/plain; charset=utf-8" },
    });
  },
};
