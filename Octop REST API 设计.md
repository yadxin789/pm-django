# PM 系统供 Octop 调用的 REST API 设计

> 版本：v1.0 ｜ 适用：得道未来 PM 项目管理系统（Django 迁移版）
> 背景：腾讯 Octop（开源多智能体 AI 助手平台）通过 `harness` 工具调用本系统 REST API，实现「AI 助手层 ↔ PM 业务系统」双向联动；PM 仍独立部署（systemd + gunicorn + celery）。表格/结构化数据强同步将来由 PM 直连各 IM 官方机器人 API（另立阶段）。

---

## 1. 总体架构与定位

```
企业微信 / 钉钉 / 飞书  ←→  Octop harness-gateway（双向消息）
                                    │  AI 智能体（LangGraph）
                                    │  调用工具（本设计：PM REST API）
                                    ▼
                          ┌─────────────────────────────┐
                          │   PM 系统（Django 独立部署）   │
                          │   本设计提供 /api/v1/*        │
                          └─────────────────────────────┘
```

- **PM 不跑在 Octop 上**（Octop 只托管 AI 助手，不承载 Django 业务应用）。
- Octop 智能体经「工具调用」访问 PM 的 REST API 读取/操作数据。
- 消息/表格推送通过 **webhook 端点**双向流转：Octop 主动推送（入站），PM 也可回调 Octop（出站）。

## 2. 鉴权方案（推荐：静态 API Token）

- 所有 `/api/v1/*` 端点要求请求头：`Authorization: Bearer <PM_API_TOKEN>`。
- 服务端配置 `settings.PM_API_TOKEN`（部署时写入 `.env`），Octop 侧在工具定义中注入该 token。
- **可选扩展**（按需）：按用户签发 JWT（`/api/v1/auth/token` 换取 user-scoped token），用于「以某用户身份」读写，权限遵循该用户角色；未传则用系统级 token（只读+管理端点）。

| 场景 | 建议 |
|---|---|
| Octop 智能体只读查询（项目/任务/消息/表格） | 系统级 Bearer Token（只读） |
| Octop 代表某用户操作（建任务/发消息） | user-scoped JWT（走 `/api/v1/auth/token`） |

统一响应格式：

```json
{ "code": 0, "message": "ok", "data": { ... } }
```
错误：`{ "code": 401, "message": "unauthorized" }`，HTTP 状态与 code 对应（200/400/401/403/404/500）。

## 3. 接口清单

### A. 认证 / 身份
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/auth/token` | 用户名+密码换 user-scoped JWT（用于代表用户操作） |
| GET | `/api/v1/me` | 当前身份（token 对应用户/角色） |
| GET | `/api/v1/users` | 用户列表（支持 `?role=&department_id=&q=`） |

### B. 项目
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/projects` | 项目列表（`?status=&creator_id=&q=&page=`） |
| GET | `/api/v1/projects/{id}` | 项目详情（含成员、进度、状态） |
| GET | `/api/v1/projects/{id}/tasks` | 项目下任务（`?status=&executor_id=`） |
| GET | `/api/v1/projects/{id}/messages` | 项目聊天记录（`?after_id=` 增量拉取） |
| POST | `/api/v1/projects/{id}/messages` | 以某用户身份向项目留言板发消息 |
| GET | `/api/v1/projects/{id}/members` | 项目成员（含企微绑定标记） |
| GET | `/api/v1/projects/{id}/progress` | 项目进度快照（供 AI 汇总汇报） |

### C. 任务
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/tasks` | 任务列表（`?status=&project_id=&executor_id=&auditor_id=&q=`） |
| GET | `/api/v1/tasks/{id}` | 任务详情（含执行人/审核人/截止日期/逾期标记） |
| POST | `/api/v1/tasks` | 创建任务（须 user-scoped token，校验权限） |
| PATCH | `/api/v1/tasks/{id}` | 更新任务（状态/进度/负责人，权限校验） |
| POST | `/api/v1/tasks/{id}/extend` | 申请/审批延期（对齐业务 `apply_extension`） |

### D. 消息 / 通知（AI 助手「读/写」）
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/notifications` | 当前用户通知（`?unread=1&page=`） |
| POST | `/api/v1/notifications/send` | 站内通知下发（title/content/接收用户或部门，管理权限） |
| GET | `/api/v1/messages/project/{project_id}` | 项目留言板历史（增量） |
| POST | `/api/v1/messages/project/{project_id}` | 项目留言板发消息 |

### E. 表格 / 结构化数据（供「推送表格」）
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/files` | 资料文件/企微收件箱文件列表（`?category=&status=`） |
| GET | `/api/v1/files/{id}` | 文件元信息 + 下载链接 |
| GET | `/api/v1/reports/project-overview` | 项目总览报表（表格化：项目/进度/逾期/成员） |
| GET | `/api/v1/reports/task-overview` | 任务汇总报表（表格化，支持 `?project_id=&status=`） |
| GET | `/api/v1/reports/export` | 导出 CSV/Excel（`?type=project|task|file`，供 Octop 直接转发到 IM 表格） |

### F. 消息 / 表格推送 webhook（入站：Octop → PM）
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/webhook/message` | 接收 Octop 推送的 IM 消息并落地为站内消息/通知 |
| POST | `/api/v1/webhook/table` | 接收 Octop 推送的表格/结构化数据（写入临时表或转通知） |
| POST | `/api/v1/webhook/event` | 通用事件订阅（任务变更、项目更新等触发 PM 侧动作） |

webhook 入站请求体统一：

```json
{
  "source": "wecom|dingtalk|feishu|octop",
  "event": "message|table|event",
  "target": { "type": "project|task|user|department", "id": 1 },
  "payload": { }
}
```
- 响应 `{"code":0}` 表示已接收；PM 侧可落库或触发异步任务（Celery）。
- 建议用 HMAC 签名（`X-PM-Signature`）防止伪造，密钥复用 `PM_API_TOKEN`。

### G. 出站 webhook（可选：PM → Octop）
PM 侧可配置出站 webhook（Octop 提供接收端点），在关键事件（任务逾期、项目完成、收到企微文件）时回调 Octop，让智能体主动推送/提醒。配置项写入 `settings.PM_OUTBOUND_WEBHOOK_URL`（可选）。

## 4. 与 Octop 的接入方式

1. **harness 工具定义**：在 Octop 的智能体工具清单中登记上述端点，每个工具=一个 `method+path`，参数映射为工具入参。
2. **鉴权注入**：工具调用时统一携带 `Authorization: Bearer <PM_API_TOKEN>`。
3. **双向消息**：
   - IM → Octop → PM：harness 收到 IM 消息，智能体调用 `POST /api/v1/messages/project/{id}` 或 `webhook/message` 落地到 PM。
   - PM → Octop → IM：PM 事件（出站 webhook）触发 Octop 智能体，由 harness 转发到企微/钉钉/飞书。
4. **表格推送**：Octop 需结构化数据时，调用 `reports/*` 取 JSON/CSV，再经 harness 组装为 IM 表格卡片推送；强同步建议将来 PM 直连官方 API。

## 5. 实施范围与后续

- **本次交付**：以上为**设计文档**；接口实现（`web/views/api.py` + `web/urls.py` 的 `/api/v1/*` 路由）待确认后实施。
- **将来扩展**：PM 直连各 IM 官方机器人 API（企微群机器人 webhook、钉钉机器人、飞书机器人 + 事件订阅），由 PM 主动推送消息/表格，不依赖 Octop。
