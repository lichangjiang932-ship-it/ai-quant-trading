---
name: fund-redeem
version: 0.1.5
description: 基金赎回模块，包含完整赎回流程：持仓查询→账户确认→赎回初始化→赎回方式选择→份额输入→手续费计算→预估到账→提交赎回→结果查询。触发场景：用户提到基金赎回、赎回流程、赎回手续费、预估到账、赎回方式、预约赎回等操作时使用此skill。
metadata: { "openclaw": { "emoji": "💸" } }
---

# 基金赎回 Skill

基金赎回完整流程，包含：**持仓查询 → 账户确认 → 赎回初始化 → 赎回方式选择 → 份额输入 → 手续费计算 → 预估到账 → 提交赎回 → 结果查询**。

---

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

## 基金锁定约束（强制执行）

**本 Skill 在处理赎回请求时，必须严格遵守以下约束，禁止违反：**

1. **基金代码锁定**：一旦确定用户要赎回的基金代码 `fundCode` 和交易账户 `transactionAccountId`，整个流程中所有 API 调用（redemption/v1/render、redemption/v2/redeem）都必须使用该 `fundCode` 和 `transactionAccountId`。
2. **允许原基金重试**：当任一 API 调用失败或返回网络/临时错误（如超时、5xx）时，**允许**使用原 `fundCode` 和 `transactionAccountId` 自动重试 1-2 次。
3. **重试时禁止修改 URL/接口**：重试时必须使用本 Skill 中给出的原始 URL 和接口，**禁止**修改 URL 中的任何参数（除重试机制本身允许的），也**禁止**使用其他 skill 中的接口来"测试"或"绕过"问题。
4. **禁止更换基金**：当 API 返回业务错误时（如份额不足、状态不允许等），**禁止** AI 自动更换为持仓中的其他基金代码或账户重试，必须将原始错误信息（status_code、message 等）原样返回给用户。
5. **禁止推测替代方案**：不允许 AI 自行推测持仓中其他可赎回的基金来"曲线救国"，不允许在用户未明确指定的情况下对其他基金进行任何赎回操作。

---

## API 调用规范

### API 基础信息

- **Base URL**: `https://trade.5ifund.com/openapi`
- **认证方式**: `Authorization: Bearer <work_token>`

### 可用 API 命令

**持仓查询**
```bash
curl -X POST "https://trade.5ifund.com/openapi/holdposition/v2/ai/list" \
  -H "Authorization: Bearer <work_token>" \
  -H "Content-Type: application/json" \
  -d '{"shareCategory": "01"}'
```

**赎回初始化**

```bash
curl -X POST "https://trade.5ifund.com/openapi/ai/redemption/v1/render" \
  -H "Authorization: Bearer <work_token>" \
  -H "Content-Type: application/json" \
  -d '{"transactionAccountId":"<transAccountId>","fundCode":"<fundCode>"}'
```

**赎回**
```bash
# 密码需先 MD5 加密
curl -X POST "https://trade.5ifund.com/openapi/ai/redemption/v2/redeem" \
  -H "Authorization: Bearer <work_token>" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "fundCode=<fundCode>&redemptionType=<redemptionType>&shareVol=<shareVol>&transActionAccountId=<transActionAccountId>"
```

---

## Step 1: 持仓查询

### curl 命令调用

```bash
curl -X POST "https://trade.5ifund.com/openapi/holdposition/v2/ai/list" \
  -H "Authorization: Bearer <work_token>" \
  -H "Content-Type: application/json" \
  -d '{"shareCategory": "01"}'
```

### 找到目标基金

- **`combineFlag = 0`**（单账户）：`transAccIdList` 即为唯一交易账户
- **`combineFlag = 1`**（多账户）：`transAccIdList` 包含多个账户ID

从 `fundPositonDetailList` 获取每个账户的：
- `transactionAccountId`：交易账户ID
- `bankName`：银行名称
- `bankAccount`：银行卡号（脱敏）
- `availableVol`：可用份额

---

## Step 2: 账户确认

展示账户选项供用户选择：

```
请选择赎回账户：
1. xx银行(尾号1212) — 可用份额 1,221.22 份
2. yy银行(尾号2233) — 可用份额 32.73 份
```

**禁止用序号如"账户1"代替银行名称展示，必须从字段读取后拼接！**

---

## Step 3: 赎回初始化

### curl 命令调用

```bash
curl -X POST "https://trade.5ifund.com/openapi/ai/redemption/v1/render" \
  -H "Authorization: Bearer <work_token>" \
  -H "Content-Type: application/json" \
  -d '{"transactionAccountId":"<transAccountId>","fundCode":"<fundCode>"}'
```

**参数说明**：
- transactionAccountId：交易账号
- fundCode：基金代码

### 关键响应字段

| 字段 | 说明 |
|------|------|
| data.fundInfo.nav | 单位净值 |
| data.fundInfo.maxRedemptionVol | 最大赎回份额 |
| data.fundInfo.minRedemptionVol | 最小赎回份额 |
| data.fundInfo.canRedeemToWallet | 是否可钱包赎回（1=可） |
| data.stepRates | 费率档位列表 |
| data.walletInfo | 钱包账户信息 |
| data.shareList | 持有份额列表 |

### 费率档位结构

| 字段 | 说明 |
|------|------|
| lwLimit | 持有天数下限 |
| upLimit | 持有天数上限（null 表示无上限） |
| rate | 费率（百分比） |

---

## Step 4: 赎回方式redemptionType选择

| 赎回方式 | 标识 | 说明 |
|----------|------|------|
| 钱包赎回 | `1` | **优先推荐**，到账最快，需 `canRedeemToWallet=1` |
| 银行卡赎回 | `0` | 到银行卡 |

**自动选择逻辑**：如果 `canRedeemToWallet=1` 且 `walletInfo.usable=1`，**自动选择钱包赎回**。

---

## Step 5: 份额输入与校验

1. **换算份额**：赎回金额 ÷ 净值 = 赎回份额
2. **可用份额检查**：份额 <= 可用份额
3. **最大/最小份额检查**
4. **保留余额检查**：赎回后剩余份额 >= `minAccountBalance`

---

## Step 6: 手续费与预估到账计算

### 有持有时间列表（先进先出）

```
1. 按持有天数倒序排列 shareHoldTimeList
2. 遍历持有时间列表，为每笔份额匹配对应费率
3. 手续费 = Σ(份额 × 费率 × 净值)
```

### 无持有时间列表

直接按 `stepRates` 匹配适用费率。

### 预估到账

```
预估到账 = 赎回金额 - 手续费
```

---

## Step 7: 提交赎回

### curl 命令调用

**赎回**
```bash
# 密码需先 MD5 加密
curl -X POST "https://trade.5ifund.com/openapi/ai/redemption/v2/redeem" \
  -H "Authorization: Bearer <work_token>" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "fundCode=<fundCode>&redemptionType=<redemptionType>&shareVol=<shareVol>&transActionAccountId=<transActionAccountId>"
```

### 关键响应字段

- `status_code`: "0000" 表示成功
- `data.appSheetSerialNo`: 单据编号

### 赎回结果展示

赎回提交后直接根据 `status_code` 判断最终状态并返回结果（无需轮询）。

**成功**（status_code=0000）：展示基金信息、赎回份额、赎回方式、手续费、预估到账金额与到账时间、单据编号。

```
====================================================================================================
                              基金赎回结果
====================================================================================================
基金代码：<fundCode>
基金名称：<fundName>
赎回账户：<银行名称(尾号XXXX)>
赎回份额：<shareVol> 份
赎回方式：<钱包赎回/银行卡赎回>
手续费：<手续费> 元
预估到账金额：<预估到账金额> 元（最终实际到账以基金公司确认为准）
预估到账时间：<预估到账时间>
单据编号：<appSheetSerialNo>
交易状态：✅ 成功
====================================================================================================
```

**注意**：赎回结果中**不展示赎回金额**；预估到账金额后**必须**追加"最终实际到账以基金公司确认为准"的提示。

**失败**（status_code 非 0000）：展示失败原因与单据编号。

```
====================================================================================================
                              基金赎回结果
====================================================================================================
交易状态：❌ 失败
失败原因：<失败原因>
单据编号：<appSheetSerialNo>
====================================================================================================
```

---

## 完整交互流程

```
1. 前置检查 → 调用 aijijin-sdk 获取 work token
2. 持仓查询 → holdings API
3. 找到基金 → 定位目标基金
4. 展示账户 → 银行(尾号XXXX)选项
5. 用户选择 → 记录 transActionAccountId
6. 赎回初始化 → redemption/v1/render API
7. 赎回方式 → 优先推荐钱包赎回
8. 输入份额 → 用户输入 shareVol
9. 换算金额 → 份额×净值
10. 计算手续费 → 按持有时间或档位计算
11. 计算预估到账 → 金额-手续费
12. 提交赎回 → redemption/v2/redeem API
13. 返回结果 → 告知成功/失败
```

---

## 注意事项

1. **持有时间列表可能为 null**（如货币基金）
2. **赎回无需单独查询结果**：赎回提交成功（status_code=0000）即表示成功，直接返回结果给用户
3. **撤单规则**：赎回提交后并非不可撤销——在当前交易日 15:00 之前可正常撤单，15:00 之后进入确认流程不可撤单。向用户说明赎回状态时，应表述为"在当前交易日 15:00 之前可撤单"，禁止表述为"提交后即不可撤销"。
