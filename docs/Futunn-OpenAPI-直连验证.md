# Futunn OpenAPI 直连验证

2026-09-23 在本机验证：`https://webapi.futunn.com` 可直连，无需 OpenD。用 AppKey + Ed25519 签名请求 `POST /api/v1.0/quote/snapshot`，`US.AAPL` 返回 HTTP 200、`ret_code=0` 和快照数据。此次只验证了只读行情；账户、模拟交易、实盘交易均未调用。

## 两套接口

- [OpenD SDK 文档](https://openapi.futunn.com/futu-api-doc/intro/authority.html)：Python `futu-api` 连接本地 OpenD；该页的登录和权限说明针对 OpenD。
- [新 OpenAPI 快速开始](https://open.futunn.com/zh-cn/api/overview/getting-started)：标准 REST/HTTP，可直接请求富途云端；支持 OAuth 2.1 + PKCE 和传统 AppKey 签名。行情与交易推送另有 WebSocket 地址。

## AppKey 只读行情复现

在富途开发者后台创建 AppKey，选择 Ed25519，并上传与本机私钥匹配的公钥。私钥放在仓库外，仅本人可读。执行：

```bash
FUTUNN_APP_KEY='<富途 AppKey ID>' \
FUTUNN_PRIVATE_KEY_FILE='/Users/kaiyi.wang/.config/stock-agent/futunn-ed25519.key' \
python3 scripts/futunn_appkey_quote_probe.py US.AAPL
```

脚本依赖 `cryptography`，只读取本地私钥并签名，不输出私钥或签名原文。请求包含 `X-Api-Key`、`X-Timestamp`、`X-Nonce` 和 `Authorization` 签名。若只得到 HTTP 200，还须检查 JSON 中的 `ret_code`；成功值为 `0`。

## OAuth 可选路线

`python3 -u scripts/futunn_oauth_quote_probe.py US.AAPL` 会临时注册 OAuth 客户端，打开授权页，通过本机 `localhost:60355/callback` 接收授权码，再换取 token 并请求同一个快照接口。用户需要亲自在授权页登录和授权。该脚本不保存 token；本次没有完成 OAuth 授权流程。

## 2026-10-13 三市场完整收盘日线验证

官方 [历史 K 线文档](https://open.futunn.com/zh-cn/api/quote/basic-data/history-kline) 的 `GET /api/v1.0/quote/{symbol}/history-kline` 已用 AppKey 直连验证，`ktype=2`、`autype=1`、`end=2026-10-12`。GET 签名最后一行为空字符串，查询参数按发出的顺序编码并参与签名。HK.00700、SH.600000、US.SPY、SH.000001、HK.800000 均返回 HTTP 200、`ret_code=0`，可取到10月12日数据及此前近一年历史。

恒生指数 HK.800000 的日线成交量为0，指数核对不要求成交量为正；不以虚构量补齐。股票仍校验正成交量和合法 OHLC。注意不同证券字段与市场时区，日 K 使用 `date` 交易日期，不将 UTC 日期当成本地交易日期。

批量请求曾遇到 HTTP 200 但 JSON `ret_code=-11`，真实等待秒数在响应头 `Retry-After` 和 `error.message`，不能只读取泛化的 `ret_msg=rate limit exceeded`。每日方向模块使用2秒请求间隔、统一退避及仓库外完整收盘日缓存。未成功校验的四只港股从样本中剔除，76/80有效覆盖高于80%门槛；保留实际覆盖数。这次未调用交易接口，也未运行 OpenD。
