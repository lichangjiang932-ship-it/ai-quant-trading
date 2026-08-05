---
name: fund-trade-revoke
version: 0.1.5
description: 交易记录撤单功能。支持购买撤单、赎回撤单、养老撤单等操作。触发场景：用户提到撤单、撤销、取消订单、revoke、cancel等操作时使用此skill。
---

# 基金撤单 Skill

基金撤单完整流程，包含：**订单详情查询 → 撤单类型判断 → 执行撤单 → 结果查询**。

## 前置 Work Token 获取（强制执行）
在调用本 Skill 的任何功能前，必须首先调用 aijijin-sdk 获取 work token。

### 获取 Token 命令

```bash
python -c "from aijijin_sdk import get_work_token; print(get_work_token())"
```

### 处理逻辑
1. 执行上述命令获取 work token
2. 如果获取失败（返回空或报错），请向用户发送提示："请检查 aijijin-sdk 配置是否正确"
3. 每次执行本 skill 都需要重新获取 token（token 有效期较短）

---

## 订单锁定约束（强制执行）

**本 Skill 在处理撤单请求时，必须严格遵守以下约束，禁止违反：**

1. **订单号锁定**：一旦确定用户要撤销的订单号 `appSheetSerialNo`，所有后续 API 调用（order/v2/ai/detail、ai/revoke）都必须使用该 `appSheetSerialNo`。
2. **允许原订单重试**：当任一 API 调用失败或返回网络/临时错误（如超时、5xx）时，**允许**使用原 `appSheetSerialNo` 自动重试 1-2 次。
3. **重试时禁止修改 URL/接口**：重试时必须使用本 Skill 中给出的原始 URL 和接口，**禁止**修改 URL 中的任何参数（除重试机制本身允许的），也**禁止**使用其他 skill 中的接口来"测试"或"绕过"问题。
4. **禁止更换订单**：当 API 返回业务错误时，**禁止** AI 自动从交易记录中挑选其他订单号进行重试，必须将原始错误信息（status_code、message 等）原样返回给用户。
5. **禁止推测替代方案**：不允许 AI 自行从交易记录中推测可撤单订单来"曲线救国"，不允许在用户未明确指定的情况下对其他订单进行任何撤单操作。

---

## API 调用规范

### API 基础信息

- **Base URL**: `https://trade.5ifund.com/openapi`
- **认证方式**: `Authorization: Bearer <work_token>`

### 可用 API 命令

**订单详情查询**
```bash
curl -X GET "https://trade.5ifund.com/openapi/order/v2/ai/detail?appSheetSerialNo=<appSheetSerialNo>" \
  -H "Authorization: Bearer <work_token>"
```

**执行撤单**
```bash
curl -X POST "https://trade.5ifund.com/openapi/ai/revoke" \
  -H "Authorization: Bearer <work_token>" \
  -H "Content-Type: application/json" \
  -d '{"operator":"999","revokeAppSheetNo":"<appSheetSerialNo>"}'
```

---

## Step 1: 订单详情查询

### curl 命令调用

```bash
curl -X GET "https://trade.5ifund.com/openapi/order/v2/ai/detail?appSheetSerialNo=<订单号>" \
  -H "Authorization: Bearer <work_token>"
```

### 关键响应字段

| 字段 | 说明 |
|------|------|
| data.appsheetserialno | 订单号 |
| data.fundCode | 基金代码 |
| data.fundName | 基金名称 |
| data.businessCode | 业务代码：022=申购, 023=赎回 |
| data.productType | 产品类型：0101=普通基金, 0102=养老基金 |
| data.cancelFlag | 撤单标志：'0'=可撤单, '1'=不可撤单 |
| data.confirmFlag | 确认状态：'0'=未确认, '1'=已撤单, '3'=确认成功, '4'=确认失败, '6'=作废 |
| data.transactionAccountId | 交易账号ID |
| data.feeSource | 资金来源：'0'=银行卡, '1'=活期, '2'=钱包 |
| data.applicationAmount | 申请金额 |
| data.walletPayAmount | 钱包支付金额 |
| data.cardPayAmount | 银行卡支付金额 |
| data.bankAccount | 银行卡账号（后4位） |
| data.bankName | 银行名称 |
| data.acceptTime | 受理时间 |
| data.exceptCfmDate | 预期确认日期 |

### 可撤单判断

- `cancelFlag = '0'`: 可撤单，显示撤单按钮
- `cancelFlag = '1'`: 不可撤单，隐藏撤单按钮

---

## Step 2: 撤单类型判断

### 撤单类型对照表

| 业务代码 (businessCode) | 产品类型 (productType) | 撤单类型 (revokeType) | 说明 |
|-------------------------|----------------------|---------------------|------|
| 022 | 0101 | '2' | 购买撤单 |
| 022 | 0102 | '6' | 养老购买撤单 |
| 023 | - | '3' | 赎回撤单 |

### revokeType 含义

| revokeType | 含义 |
|-----------|------|
| '2' | 购买撤单 |
| '3' | 赎回撤单 |
| '6' | 养老撤单 |

---

## Step 3: 执行撤单

### curl 命令调用

```bash
curl -X POST "https://trade.5ifund.com/openapi/ai/revoke" \
  -H "Authorization: Bearer <work_token>" \
  -H "Content-Type: application/json" \
  -d '{"operator":"999","revokeAppSheetNo":"<appSheetSerialNo>"}'
```

### 关键响应字段

- `status_code`: "0000" 表示成功
- `message`: 响应信息

---

## Step 4: 结果查询

### 响应判断

- `status_code = "0000"`: 撤单成功
- `status_code ≠ "0000"`: 撤单失败，展示错误信息可重试

---

## 完整交互流程

```
1. 前置检查 → 调用 aijijin-sdk 获取 work token
2. 收集信息 → 用户提供订单号
3. 订单详情查询 → 调用 order_detail API
4. 可撤单判断 → 检查 cancelFlag，'0'=可撤单，'1'=不可撤单
5. 撤单类型判断 → 根据 businessCode + productType 确定 revokeType
6. 执行撤单 → 调用 revoke API
7. 结果查询 → 判断 status_code 是否为 "0000"
8. 返回结果 → 告知最终状态
```
