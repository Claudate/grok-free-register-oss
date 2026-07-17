// ============================================================
//  Cloudflare Email Worker — 把收到的邮件 POST 到你的 webhook
//  用于 grok-free-register 的「自建邮箱模式」(EMAIL_MODE=custom)
//
//  链路: 发件方 → Cloudflare Email Routing → 本 Worker → 你的 webhook(email_server.py)
//
//  ⚠️ 重要(踩过的坑):env.WEBHOOK_URL 必须是【域名】,不能用【裸 IP】!
//     Cloudflare Workers 的 fetch 不允许直接请求裸 IP,会返回
//     "error code: 1003 (Direct IP access not allowed)" 并被静默吞掉
//     —— Worker 看起来成功、你的服务器却收不到任何请求。
//     做法:给服务器 IP 加一条 DNS A 记录(如 hook.example.com,灰云/DNS-only),
//     WEBHOOK_URL 填 http://hook.example.com:8080/webhook。
//
//  部署:
//    npm create cloudflare@latest cf-mail-webhook
//    cd cf-mail-webhook && npm i postal-mime
//    # 用本文件替换 src/index.js
//    npx wrangler secret put WEBHOOK_URL      # 输入 http://hook.example.com:8080/webhook
//    npx wrangler secret put WEBHOOK_TOKEN    # 可选,鉴权用
//    npx wrangler deploy
//  然后在 Cloudflare 后台:Email → Email Routing → Routing rules →
//    Catch-all → 动作选「Send to a Worker」→ 选本 Worker。
//  (子域名收信需在 Email Routing 单独启用该子域并配它自己的 catch-all。)
// ============================================================
import PostalMime from "postal-mime";

function trim(s, max = 20000) {
  if (!s) return "";
  return s.length > max ? s.slice(0, max) + "\n...[truncated]" : s;
}

export default {
  async email(message, env, ctx) {
    if (!env.WEBHOOK_URL) {
      console.error("WEBHOOK_URL 未配置 (npx wrangler secret put WEBHOOK_URL)");
      return;
    }
    const parsed = await PostalMime.parse(message.raw);
    const payload = {
      from: message.from,
      to: message.to,
      subject: parsed.subject || message.headers.get("subject") || "",
      text: trim(parsed.text || ""),
      html: trim(parsed.html || ""),
    };

    const headers = { "content-type": "application/json" };
    if (env.WEBHOOK_TOKEN) headers["x-webhook-token"] = env.WEBHOOK_TOKEN;

    const res = await fetch(env.WEBHOOK_URL, {
      method: "POST",
      headers,
      body: JSON.stringify(payload),
    });
    // 日志只保留状态码，避免把 webhook 地址或上游响应写入 Worker 日志。
    console.log(`webhook delivery -> ${res.status}`);
    if (!res.ok) {
      console.error(`webhook delivery failed: ${res.status}`);
    }
  },
};
