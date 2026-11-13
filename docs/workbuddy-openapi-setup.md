# WorkBuddy Open API 申请与接入准备

核查日期：2026-11-08。当前没有创建开发者应用、提交申请、接收密钥或部署 OAuth 回调。本项目当前采用的桌面 MCP 派工连接不需要开发者应用；本文为后续可选 Open API 通道保留申请与开发步骤。

1. 打开 [WorkBuddy 开放平台](https://open.workbuddy.cn/)，由本人登录、入驻并完成要求的开发者／企业资质认证。资格与可创建的应用类型以本人控制台为准。[官方入驻流程](https://open.workbuddy.cn/docs)
2. 按真实用途创建应用，填写名称、介绍、权限用途和回调地址。官方第三方接入流程提供「硬件接入」并要求审核；它面向硬件厂商／合作伙伴。个人派工台如不符合该类型，应先向平台确认适用入口，不能伪报硬件身份；Open API 准备说明也列举 Buddy 应用，是否开放所需权限仍需确认。积分兑换应用不适合本功能。[应用类型与审核流程](https://open.workbuddy.cn/docs/third-party-app)
3. 仅申请下表两个 Scope，并分别说明必要性。建议用途描述：由本人从已授权的派工台向自己的 PC 本地助理发起消息，并查看在线状态与对应消息；任何成果仍须独立检查和本人复审。[官方权限列表](https://open.workbuddy.cn/docs/third-party-app#权限配置)

| Scope | 本项目用途 |
| --- | --- |
| `user.localassistant.readable` | 查询本地助理在线状态与消息历史 |
| `user.localassistant.invokable` | 向本地助理发送本人授权的任务消息 |

4. 准备一个真实可达的回调地址，例如 `https://<已验证的派工台域名>/oauth/workbuddy/callback`，完成部署后再登记。该路径**目前尚未实现**，示例不能直接当作已可用地址。本项目要求有效 HTTPS；尚无依据可依赖 localhost/HTTP 例外。登记值、授权请求和换码请求中的 `redirect_uri` 必须完全一致。[官方 OAuth 参数](https://open.workbuddy.cn/docs/openapi#认证授权-api)
5. 由本人检查并提交应用及 Scope 审核；启用后再进行用户 OAuth 授权。平台说明应用密钥仅明文展示一次。密钥只应交给受控服务端，不能放进浏览器、前端配置、聊天、Git、任务提示或普通桌面客户端代码。[应用凭据说明](https://open.workbuddy.cn/docs/third-party-app#应用说明)

本项目拟采用的授权流程是：本机私有后端生成随机 `state` 并绑定当前授权会话 → 浏览器打开官方授权页 → 后端校验回调 `state` → 后端用一次性 `code`、完全一致的 `redirect_uri`、`client_id` 和 `client_secret` 换码 → 后端按实际授权 Scope 调用接口。换码／刷新使用官方 HTTPS `/openapi/v2/token`，表单编码。官方明确禁止向前端或客户端代码暴露 `client_secret`。[OAuth 接口合同](https://open.workbuddy.cn/docs/openapi#认证授权-api)

这里的“本机私有后端”是本人控制的服务器进程，不是把共享密钥打包到可分发的客户端。后续实现应通过该用户独占的凭据存储或 DPAPI 保护密钥与刷新令牌；中转服务器如只转发回调，不应持有它们。回调查询参数、令牌和授权头需要从访问日志、错误信息中去除。以上是待实现设计，本次未写入任何授权或令牌文件。令牌失效时间应依据响应中的 `expires_in`，不要写死：官方两页对有效期的示例／说明存在差异。[换码响应](https://open.workbuddy.cn/docs/openapi#认证授权-api)、[令牌说明](https://open.workbuddy.cn/docs/third-party-app#授权认证)

现有代码与缺口：

| 位置／接口 | 已准备的能力 | 仍需完成 |
| --- | --- | --- |
| `dispatcher/workbuddy_openapi.py` → `build_authorization_url(...)` | 生成最小 Scope 的官方授权地址；校验 HTTPS 回调、要求 `state`；不打开浏览器 | 后端授权会话、随机 state 保存／一次性校验、回调路由 |
| `WorkBuddyOpenAPI(access_token)` | 由宿主显式传入令牌，仅访问固定官方 HTTPS 地址，不读取桌面认证文件 | 换码、刷新、私有凭据存储、过期与撤销处理、授权 Scope 核对 |
| `get_local_assistant_status()` | 返回本地助理在线布尔值 | 无设备身份确认能力，不能据此断言连接的是哪台 PC |
| `send_text(content)` | 返回 `AcceptedMessage(message_id, delivery_state='accepted')` | 没有任务完成、取消、指定工作区、模型或思考强度的可靠执行合同 |
| `get_messages(...)` | 分页／按消息游标读取历史；保留原始内容，不推断执行完成 | 跨消息任务关联、可信终态与产物验证不能靠文本“完成了”代替 |
| `tests/test_workbuddy_openapi.py` | 使用模拟 HTTPS 响应测试校验与错误边界 | 尚未使用真实开发者应用或令牌联调 |

上述表格的“已准备”来自本地代码检查。官方本地助理接口只公开在线状态、发消息和消息历史；云端 `/tasks` 与 ACP 属另一种能力，不能当成本机任务的终态接口。当前客户端有意不实现工具审批回复，不替用户批准操作；发送超时会保留“可能已接收”的不确定状态，不盲目重发。[本地助理与云端接口](https://open.workbuddy.cn/docs/openapi#本地助理-api)

应用获批后的首轮验证应从只读在线状态开始，再由本人明确授权发送一条隔离测试消息；仅在真实产物、独立检查及本人复审全部通过后，才允许完成滴答任务。Open API 的消息 ID 本身始终只表示接收回执。
