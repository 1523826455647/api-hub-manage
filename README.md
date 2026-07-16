# 中转站渠道整合管理工具

面向 NewAPI / Sub2API 上游站点的集中监控与管理面板。

统一管理多个 NewAPI / Sub2API 中转站账号，一站式查看余额、消耗、分组倍率，支持兑换码兑换。

## 快速开始

```bash
cd api-hub-manager

# 安装依赖
pip install -r requirements.txt

# 启动服务
python main.py
```

或双击 `start.bat` 启动。启动后访问 **http://localhost:8899**

## 功能

### 仪表盘
- 总余额汇总、今日消耗、累计消耗
- 异常渠道数量统计
- 所有渠道概览卡片

### 渠道管理
- 支持 **NewAPI** (one-api/new-api 系列) 和 **Sub2API** 两类上游
- 支持 Token/Cookie 认证和账号密码登录两种方式
- Token 刷新（重新登录获取新凭据）
- 删除渠道

### 数据查询
- **余额查询**: 自动从 `/api/status` 获取 `quota_per_unit` 精确换算
- **分组倍率**: NewAPI 使用 `/api/user/self/groups`，Sub2API 使用 `/api/v1/groups/available` + `/api/v1/groups/rates`
- **消耗统计**: 今日消耗 + 累计消耗（分别调用日志统计和用户信息接口）
- **模型列表**: 获取可用模型

### 兑换码
- NewAPI: 调用 `/api/user/topup`
- Sub2API: 调用 `/api/v1/redeem`

## API 端点对照

### NewAPI 端点
| 接口 | 用途 |
|------|------|
| GET /api/status | 获取 quota_per_unit、Turnstile 配置 |
| POST /api/user/login | 用户名 + 密码登录 |
| GET /api/user/self | 用户信息（quota、used_quota、group） |
| GET /api/user/self/groups | 分组列表及倍率 |
| GET /api/log/self/stat | 日志统计（今日消耗） |
| POST /api/user/topup | 兑换码兑换 |

### Sub2API 端点
| 接口 | 用途 |
|------|------|
| POST /api/v1/auth/login | 邮箱 + 密码登录 → access_token |
| POST /api/v1/auth/refresh | 刷新 token |
| GET /api/v1/auth/me | 用户信息（balance） |
| GET /api/v1/groups/available | 可用分组 |
| GET /api/v1/groups/rates | 分组倍率覆盖 |
| GET /api/v1/usage/dashboard/stats | 消耗统计 |
| GET /api/v1/announcements | 公告 |
| POST /api/v1/redeem | 兑换码兑换 |

## 本项目 API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /api/dashboard | 仪表盘汇总 |
| GET | /api/accounts | 列出所有渠道 |
| POST | /api/accounts | 添加渠道 |
| DELETE | /api/accounts/{id} | 删除渠道 |
| GET | /api/accounts/{id}/balance | 余额 |
| GET | /api/accounts/{id}/groups | 分组倍率 |
| GET | /api/accounts/{id}/models | 模型列表 |
| GET | /api/accounts/{id}/usage | 消耗统计 |
| GET | /api/accounts/{id}/overview | 完整概览 |
| POST | /api/accounts/{id}/refresh | 刷新 Token |
| POST | /api/accounts/{id}/redeem | 兑换码 |

## 技术栈

- Python 3.11+
- FastAPI + Uvicorn
- httpx（异步 HTTP）
- Jinja2 模板
- 前端纯 HTML/CSS/JS（无框架）

## 功能状态

- ✅ 余额/消耗/分组倍率查询
- ✅ 兑换码兑换
- ✅ Token 刷新
- ✅ Cloudflare Turnstile 自动打码（CapSolver）
- ✅ 自动刷新（5/10/30 分钟可选）
- ✅ 消费对比图表
- ✅ 模型分类关键词可自定义
- ✅ 内存缓存（避免频繁请求上游）
- ❌ 暂无通知推送（Telegram/Webhook 等）
- ❌ 暂无 API Key 管理
- ❌ 暂无充值/订阅
