# 手机远程访问

AuToDo 可以通过 HTTPS 和 SSH 反向隧道提供手机访问。以下示例使用 `autodo.example.com` 和 SSH 主机别名 `autodo-relay`，部署时换成自己的域名和别名。

手机请求通过 nginx 的 HTTPS 入口进入中转服务器，再转发到中转的 `127.0.0.1:18766`。AuToDo 主动建立 SSH 隧道，把请求交给电脑的远程鉴权端口 `127.0.0.1:8766`。任务、工作区、密码哈希和会话数据库保留在电脑。本机看板仍可通过 `127.0.0.1:8765` 打开。

## 配置电脑

1. 在 Windows 当前用户的 OpenSSH 配置中声明 `autodo-relay` 主机别名，指向中转服务器的普通 SSH 账户。先验证主机指纹，配置可供该用户非交互使用的 SSH 密钥。
2. 生成至少 32 个字符的随机代理密钥，保存在项目 `data/relay-proxy.key`，只授予当前用户所需访问权限。服务端使用同一值。
3. 在项目的 `config.local.json` 中配置：

```json
{
  "remote_access": {
    "enabled": true,
    "mode": "password_proxy",
    "port": 8766,
    "origin": "https://autodo.example.com",
    "proxy_secret_file": "data/relay-proxy.key",
    "tunnel_enabled": true,
    "ssh_alias": "autodo-relay",
    "relay_port": 18766
  }
}
```

## 配置中转服务器

1. 将自己的域名解析到中转服务器，签发 TLS 证书。按实际证书路径和域名调整 [nginx 模板](../deploy/nginx/autodo.conf.example)。
2. 创建模板引用的 `/etc/autodo/proxy-secret.conf`。文件仅由服务器管理员管理，内容为 `proxy_set_header X-Dispatcher-Proxy-Secret "<与电脑相同的随机密钥>";`，将占位符换成真实值。测试 nginx 配置后重新加载。
3. SSH 账户须能做远程端口转发。确保中转只在 `127.0.0.1:18766` 接收隧道转发，公网入口使用 HTTPS。

## 启动和登录

启动 `AuToDo.cmd` 或 `scripts/start.ps1`。隧道随服务运行，断线后自动退避重连。保持电脑开机、联网并运行服务，手机即可通过配置的 HTTPS 域名访问。

首次设置时，在本机项目目录运行 `python -m dispatcher mobile-setup`，用 30 分钟内有效的一次性链接设置 `autodo` 账号的密码。新邀请会使旧邀请失效。创建账号后可用密码登录，右上角「退出」会撤销当前浏览器会话。

若手机看到 503，可检查本机服务状态、`data/relay-tunnel.log` 和中转服务器的回环监听。若登录失败，核对 `origin` 与实际 HTTPS 入口是否一致。`config.local.json`、代理密钥、邀请链接和 `data/web-auth.db` 均属本机私有数据。
