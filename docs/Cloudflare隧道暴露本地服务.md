# 用 Cloudflare Tunnel 让公网访问本地报告页

Cloudflare 不能直接连到你电脑，需要在本机跑 **Cloudflare Tunnel**（`cloudflared`）：由本机主动连到 Cloudflare，再把公网请求转发到本地服务。无需公网 IP、不用在路由器上开端口。

---

## 方式一：快速隧道（免登录，适合临时分享）

**1. 安装 cloudflared**

- **macOS**：`brew install cloudflared`
- **Windows**：从 [Cloudflare 下载页](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/) 下载并加入 PATH
- **Linux**：见官方 [安装说明](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/)

**2. 先启动本地服务**

```bash
python server.py
# 确保 http://127.0.0.1:8000 可访问
```

**3. 再开一个终端，启动隧道**

```bash
cloudflared tunnel --url http://127.0.0.1:8000
```

终端会打印类似：

```text
Your quick Tunnel has been created! Visit it at:
https://xxxx-xx-xx-xx-xx.trycloudflare.com
```

用浏览器打开这个 `https://...trycloudflare.com` 即可访问你本地的服务。例如：

- 根路径：`https://xxxx.trycloudflare.com/`
- 报告在线页：`https://xxxx.trycloudflare.com/report/page`
- 接口文档：`https://xxxx.trycloudflare.com/docs`

**说明**：每次重新执行 `cloudflared tunnel --url ...` 会得到新的随机域名；关掉隧道或断网后，该链接失效。

### 快速隧道的域名能持续多久？

- **没有固定时长限制**：只要本机的 `cloudflared` 进程一直在跑，这个 `https://xxxx.trycloudflare.com` 的域名就一直可用，官方没有写「几小时后失效」之类的上限。
- **何时失效**：一旦你关掉隧道（Ctrl+C 或 `pkill`）、本机断网、休眠/关机，该链接立刻失效；下次再开隧道会**重新分配一个新的随机域名**，旧链接不能复用。
- **无 SLA**：快速隧道仅供测试/临时分享，Cloudflare 不保证可用性和 uptime，不适合当长期生产地址。
- **要固定域名、长期用**：需用 **方式二** 在 Cloudflare 账号下创建命名隧道并绑定自己的域名（如 `report.yourdomain.com`）。

### 开启与关闭隧道

**开启隧道**

- **前台运行**（当前终端会一直占用，关掉终端或按 Ctrl+C 即关闭隧道）：
  ```bash
  cloudflared tunnel --url http://127.0.0.1:8000
  ```
  终端会打印公网 URL（如 `https://xxxx.trycloudflare.com`），用浏览器打开该地址下的 `/report/page` 即可。
- **后台运行**（关掉终端后隧道仍运行，需用下面「关闭」命令结束）：
  ```bash
  cloudflared tunnel --url http://127.0.0.1:8000 &
  ```
  或使用 `nohup` 避免被挂断：
  ```bash
  nohup cloudflared tunnel --url http://127.0.0.1:8000 > /tmp/cloudflared.log 2>&1 &
  ```
  公网 URL 会写在 `/tmp/cloudflared.log` 里（搜索 `trycloudflare.com`）。

**关闭隧道**

- **前台运行**：在当前终端按 **Ctrl+C** 即可关闭。
- **后台运行**：先查到进程再结束：
  ```bash
  # 查进程（记下 PID 或直接用下面一条命令结束）
  ps aux | grep cloudflared

  # 结束所有「快速隧道」进程（只杀 tunnel --url 这类，不影响已登录的命名隧道）
  pkill -f "cloudflared tunnel --url"
  ```
  或按 PID 结束：`kill <PID>`（例如 `kill 12345`）。

**本地服务**

- 隧道只是转发流量，本机必须先启动服务：
  ```bash
  python server.py
  ```
- 关闭本地服务：在运行 `server.py` 的终端按 **Ctrl+C**，或 `pkill -f "python server.py"`。

---

## 方式二：自定义域名（需 Cloudflare 账号，适合长期用）

1. 在 [Cloudflare Dashboard](https://dash.cloudflare.com/) 添加你的域名并接管 DNS。
2. 安装并登录 cloudflared：
   ```bash
   cloudflared tunnel login
   # 按提示在浏览器里完成授权
   ```
3. 创建命名隧道并配置转发到本地 8000 端口，例如：
   ```bash
   cloudflared tunnel create stock-agent
   # 编辑 ~/.cloudflared/config.yml，指定 ingress 为 http://localhost:8000
   cloudflared tunnel route dns stock-agent report.yourdomain.com
   cloudflared tunnel run stock-agent
   ```
4. 之后用 `https://report.yourdomain.com` 访问（需在 Cloudflare 里把域名指到该隧道）。

具体步骤以官方文档为准：[Connect networks with Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/)。

---

## 注意

- 快速隧道有并发请求限制（例如 200），且不支持 SSE，适合自用或小范围分享报告页。
- 隧道只是「把公网流量转到本机」，本机必须先运行 `python server.py`（或你实际用的端口），否则访问会失败。
- 若要把「前端页面 + 后端 API」都部署到 Cloudflare 上长期运行（而不是暴露本机），需要把后端迁到 Cloudflare Workers 或其它托管，再配合 Pages/静态资源，可另行规划。

---

## 阿里云 / 国内有类似服务吗？

**阿里云**：没有和 Cloudflare「快速隧道」一模一样的官方产品。阿里云有 **VPN 网关**（企业 VPC 互通）、**会话管理端口转发**（访问无公网 ECS）等，都是云内网/企业场景，不是「一条命令把本机 localhost 暴露到公网」。若要用阿里云做内网穿透，一般是自己在 ECS 上搭 **frp** 等服务端，再在本地跑客户端。

**国内类似「一条命令暴露本地」的服务**（用法接近 Cloudflare quick tunnel / ngrok）：

| 服务 | 说明 | 免费/付费 |
|------|------|------------|
| **NATAPP** (natapp.cn) | 国内机房，基于 ngrok。免费为随机域名、可能不定期更换；付费约 ¥9–18/月 可固定域名。 | 免费试用 + 付费 |
| **cpolar** (cpolar.com) | 类似 ngrok，有客户端。免费版为随机 URL、重启会变。 | 免费 + 付费 |
| **花生壳** (oray.com) | 老牌 DDNS + 内网穿透，需装客户端，有免费额度。 | 免费额度 + 付费 |
| **ngrok** (ngrok.com) | 国外服务，国内可直接用但延迟看网络。免费版随机域名。 | 免费 + 付费 |
| **frp** (gofrp.org) | 开源，需自备一台有公网 IP 的服务器（如阿里云 ECS）部署服务端，本地跑客户端。 | 免费（自建） |

**选择建议**：  
- 想**国内访问快、少折腾**：试 **NATAPP** 或 **cpolar**，注册后按文档装客户端、一条命令映射本地端口即可。  
- 想**完全免费、可接受自建**：用 **frp**，在阿里云/腾讯云买一台最低配 ECS 做服务端，本机跑 frp 客户端。  
- **Cloudflare 快速隧道**：免登录、零配置，适合临时分享；在国内访问可能稍慢或受网络影响，但无需备案、无需国内账号。
