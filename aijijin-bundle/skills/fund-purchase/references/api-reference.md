# 基金申购 API Reference

本文件集中维护 `fund-purchase` 的接口、认证和关键字段。业务流程以 `SKILL.md` 为准。

## 认证

所有 `trade.5ifund.com/openapi` 接口使用：

```http
Authorization: Bearer <work_token>
```

每次执行申购流程前必须重新获取 Work Token：

```bash
python -c "from aijijin_sdk import get_work_token; print(get_work_token())"
```

如果首次获取失败（返回空或报错），自动重试 1 次；仍失败时提示：`请检查 aijijin-sdk 配置是否正确`。

## Token 失效自动刷新

任一需要认证的接口返回 HTTP `401`，或响应状态/消息明确表示 Work Token 已失效、过期或未授权时：

1. 立即重新调用 `aijijin-sdk` 获取新 Token。
2. 保留失败请求的 HTTP 方法、URL、查询参数、请求体和除 `Authorization` 外的请求头，仅替换认证头。
3. 使用新 Token 自动重试原请求；单个请求最多刷新并重试 2 次。
4. 仅当 Token 重新获取失败，或刷新重试 2 次后仍明确返回 Token 失效时，才停止当前操作并返回认证错误。
5. 基金不存在、参数错误、余额/份额不足、交易状态限制等业务错误不得按 Token 失效处理。

## 接口清单

### 1. 申购初始化

```bash
curl -X GET "https://trade.5ifund.com/openapi/ai/subscribe/init?fundCode=<fundCode>" \
  -H "Authorization: Bearer <work_token>"
```

关键字段：

| 字段 | 说明 |
|---|---|
| data.custId | 客户 ID，用于协议阅读记录接口 |
| data.paramOpenFundAccBean.fundCode | 基金代码 |
| data.paramOpenFundAccBean.fundName | 基金名称 |
| data.minBuy | 最小起购金额（新申购） |
| data.paramOpenFundAccBean.minAddBuy | 最小追加金额 |
| data.maxBuy | 单笔最大购买金额 |
| data.fundRiskLevel | 产品风险等级（1-5） |
| data.ov_clientriskrate | 客户风险等级（1-5） |
| data.ov_flag | 风险评测状态标志 |
| data.bankBuyDiscount | 银行卡购买折扣 |
| data.moneyToStockBuyDiscount | 钱包购买折扣 |
| data.moneytostockTzeroFlag | 是否支持钱包支付（0=不支持，仅银行卡；1=支持，钱包+银行卡） |
| data.fundtzeroList | 钱包账户列表 |
| data.bankCardSplitListResult | 银行卡账户列表 |
| data.subOrAddResult | 已申购过该基金的账户（追加申购判断） |
| data.paramOpenFundAccBean.isRollingHold | 是否滚动持有（1=是，0=否） |
| data.paramOpenFundAccBean.hasLockPeriod | 是否有封闭锁定期（1=是，0=否） |
| data.paramOpenFundAccBean.hasRedeemDate | 是否有赎回日期（1=是，0=否） |
| data.paramOpenFundAccBean.buyUrl | 购买 URL（`ren`=新基金） |
| data.paramOpenFundAccBean.appkday | 申请日（T 日，YYYYMMDD） |
| data.paramOpenFundAccBean.confirmDay | 预计确认日（T+1，YYYY-MM-DD） |
| data.accountValidateResult.validateCode | 个人信息校验结果码 |
| data.accountValidateResult.validateMessage | 个人信息校验提示信息 |

### 2. 基金费用信息查询

```bash
curl -s "https://fund.10jqka.com.cn/interface/fund/tradeRule/<fundCode>"
```

关键字段：

| 字段 | 说明 |
|---|---|
| data.rateInfo.glf | 管理费 |
| data.rateInfo.tgf | 托管费 |
| data.rateInfo.fwf | 销售服务费 |
| data.rateInfo.sg.qd[].money | 申购金额区间 |
| data.rateInfo.sg.qd[].rate | 原始申购费率 |
| data.rateInfo.sh | 赎回费率 |

申购金额区间必须使用费用查询接口返回的 `money` 字段；申购费率只展示 `rate`，不展示折后费率 `irate`。

### 3. 客户状态查询（可选补充）

`getCustAccoStatus` 可能返回 404 路由异常，不可作为主依赖。init 返回的 `accountValidateResult` 是个人信息校验主来源。本接口能调通时仅用于补充风险测评过期时间等信息。

```bash
curl -X POST "https://trade.5ifund.com/openapi/rz/account/dubbo/accountInfo/getCustAccoStatus" \
  -H "Authorization: Bearer <work_token>" \
  -H "Content-Type: application/json" \
  -d '{"version":"VOCATIONCODE_22"}'
```

### 4. 协议查询

```bash
curl -s "https://trade.5ifund.com/tohangqing/interface/fund/detail/<fundCode>_tradeInitTreaty" \
  -H "Authorization: Bearer <work_token>"
```

关键字段：

| 字段 | 说明 |
|---|---|
| data.tradeInitTreaty | 本轮需展示并确认的基金协议列表 |
| data.tradeInitTreaty[].title | 协议名称 |
| data.tradeInitTreaty[].jumpAction | 协议跳转链接 |

链接解析规则：

- `action=openpdf,url=<URL>,filesize=...`：提取 `url=` 后的真实 URL。
- `http://...` 或 `https://...`：直接作为 URL。

### 5. 协议阅读记录

用户明确回复 `已阅读` 后、签约检查前必须调用：

```bash
curl --location --request POST 'https://trade.5ifund.com/openapi/record/trade' \
  --header 'Authorization: Bearer <work_token>' \
  --header 'Content-Type: application/json' \
  --data-raw '{
    "custId": "<custId>",
    "agreements": <confirmedAgreements>,
    "sourceType": "BUY"
  }'
```

成功判定：

- HTTP 请求成功，且响应中 `status_code=0000` 或 `error.id=0` 时，视为记录成功。
- 其他业务失败状态视为失败，展示响应中的 `status_msg`、`message` 或 `error.msg`，并停止后续流程。

### 6. 签约检查

```bash
curl -X POST "https://trade.5ifund.com/openapi/ai/sign_contract/v1/check_before_trade" \
  -H "Authorization: Bearer <work_token>" \
  -H "Content-Type: application/json" \
  -d '{"applicationAmount":"<amount>","fundCode":"<fundCode>","transactionAccountId":"<transAccountId>"}'
```

### 7. 提交订单

```bash
curl -X POST "https://trade.5ifund.com/openapi/ai/buy" \
  -H "Authorization: Bearer <work_token>" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "buyType=<buyType>&fundCode=<fundCode>&money=<amount>&transactionAccountId=<transAccountId>"
```

`buyType` 按用户选择的支付方式取值：钱包账户 `1`，银行卡账户 `0`。钱包支付时必须保持 `buyType=1`。

关键字段：

| 字段 | 说明 |
|---|---|
| status_code | `0000` 表示接口提交成功，不等同于最终订单成功 |
| data.appSheetSerialNo | 订单号 |

### 8. 查询订单详情

```bash
curl -X POST "https://trade.5ifund.com/openapi/order/v2/ai/detail" \
  -H "Authorization: Bearer <work_token>" \
  -H "Content-Type: application/json" \
  -d '{"appSheetSerialNo":"<appSheetSerialNo>"}'
```

关键字段：

| 字段 | 说明 |
|---|---|
| data.acceptTime | 申请下单时间，格式 `yyyy-MM-dd HH:mm:ss` |
| data.fundCode | 基金代码 |
| data.fundName | 基金名称 |
| data.exceptCfmDate | 预计确认时间 |
| data.applicationAmount | 申请金额 |
| data.feeSource | 资金来源（0=银行卡/1=活期/2=钱包） |
| data.bankName | 银行名称 |
| data.bankAccount | 银行卡尾号 |
| data.confirmFlag | 订单状态标识，仅内部判断用 |
| data.checkFlag | 订单检查标识，仅内部判断用 |
| data.failMsg.thsMessage | 失败原因优先展示字段 |
| data.failMsg.message | 失败原因兜底展示字段 |