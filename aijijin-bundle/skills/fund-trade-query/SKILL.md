---
name: fund-trade-query
version: 0.1.5
description: 基金交易记录查询与分析。支持游标分页、交易详情补全、买卖、分红、基金转换及净值估值。用户提到交易记录、买入、卖出、赎回、分红、红利再投、基金转换、交易明细、订单查询或交易盈亏时使用此 Skill。
---

# 基金交易查询 Skill

## 功能说明

| 功能 | 说明 |
|------|------|
| 交易列表 | 分页查询用户的基金交易记录 |
| 交易详情 | 根据订单号查询单条交易详情 |
| 交易分析 | 分析买卖、分红、基金转换、持仓市值和盈亏 |

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

## API 调用规范

### API 基础信息

- **Base URL**: `https://trade.5ifund.com/openapi`
- **认证方式**: `Authorization: Bearer <work_token>`

### 可用 API 命令

**交易列表查询（首页）**
```bash
curl -X POST "https://trade.5ifund.com/openapi/order/v2/ai/orderlist" \
  -H "Authorization: Bearer <work_token>" \
  -H "Content-Type: application/json" \
  -d '{"custId":"<custId>","offset":1,"limit":20,"startDate":"<startDate>","endDate":"<endDate>","opBusinessCode":"all","opProductType":"all","queryProcessing":false}'
```

**交易详情查询**
```bash
curl -X POST "https://trade.5ifund.com/openapi/order/v2/ai/detail" \
  -H "Authorization: Bearer <work_token>" \
  -H "Content-Type: application/json" \
  -d '{"appSheetSerialNo":"<appSheetSerialNo>"}'
```

---

## API信息

**Base URL**: `https://trade.5ifund.com/openapi`

### 1. 交易列表

```
POST /order/v2/ai/orderlist
```

### 2. 交易详情

```
POST /order/v2/ai/detail
```

---

## 请求参数

### 交易列表必填参数

| 参数 | 类型 | 说明 |
|------|------|------|
| custId | String | 客户 ID |
| offset | Integer | 页码，首页传 `1` |
| startDate | String | 开始日期，格式 `yyyyMMdd` |
| endDate | String | 结束日期，格式 `yyyyMMdd` |
| queryProcessing | Boolean | 是否查询进行中交易 |

### 交易列表可选参数

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| limit | Integer | 20 | 每页条数，最大 200 |
| opBusinessCode | String | all | 交易类型：`buy`、`sell`、`aip`、`change`、`dividend`、`other`、`all` |
| opProductType | String | all | 产品类型：`demand`、`time`、`normal_fund`、`advanced_financing`、`pension` |

### 游标参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| lastAcceptTime | String | `offset != 1` 时必填 | 当前页最后一条数据返回的同名字段 |
| lastAppSheetSerialNo | String | `offset != 1` 时必填 | 当前页最后一条数据返回的同名字段 |

`startDate` 和 `endDate` 在正常调用中应显式传入。服务端未传这两个参数时，默认查询最近 30 天。

### 分页逻辑（重点）

1. 首页使用 `offset = 1`，不要传 `lastAcceptTime` 和 `lastAppSheetSerialNo`。
2. 下一页将 `offset` 加 1，并同时传入当前页最后一条数据的 `lastAcceptTime` 和 `lastAppSheetSerialNo`。
3. `offset != 1` 时两个游标参数缺一不可；参数不完整会退化为首页查询，造成重复数据。
4. 每次请求成功后应立即保存当前页，再继续请求下一页。

下一页请求体示例：

```json
{
  "custId": "CUST001",
  "offset": 2,
  "startDate": "20250601",
  "endDate": "20250626",
  "queryProcessing": true,
  "lastAcceptTime": "2025-06-25 15:30:00",
  "lastAppSheetSerialNo": "20250625000001"
}
```

### 交易详情

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| appSheetSerialNo | string | ✅ | 订单号 |

---

## 响应关键字段

### 交易列表 data[]

| 字段名 | 说明 | 可选值 |
|--------|------|--------|
| firstBusinessType | 一级交易类型 | buy（买入）, aip（定投）, sell（卖出）, change（转换）, dividend（分红）, other（其它） |
| firstBusinessTypeMsg | 一级交易类型描述 | 买入, 定投, 卖出, 转换, 分红, 其它 |
| subBusinessType | 二级交易类型 | card_subscription（普通申购∈买入）, card_reservation（普通认购∈买入）, wallet_subscription（钱包申购∈买入）, wallet_reservation（钱包认购∈买入）, demand_subscription（活期申购∈买入）, demand_reservation（活期认购∈买入）, award（活动奖励∈买入）, refund（基金退款∈买入）, income（基金收益∈买入）, fund_sold（基金卖出（到活期）∈买入）, card_aip（普通定投∈定投）, wallet_aip（钱包定投∈定投）, demand_aip（活期定投∈定投）, redemption_to_card（赎至银行卡∈卖出）, forced_redemption（强行赎回∈卖出）, redemption_to_wallet（赎至钱包∈卖出）, redemption_to_demand（赎至活期∈卖出）, fast_redemption（快速取现∈卖出）, redemption_for_buying（（从活期赎回）用于买基金∈卖出）, dividend_reinvestment（红利再投∈分红）, cash_dividend（现金分红∈分红）, normal_change（普通转换∈转换）, forced_increase（强行增加∈其它）, forced_decrease（强行减少∈其它） |
| subBusinessTypeMsg | 二级交易类型描述 | 普通申购, 普通认购, 钱包申购, 钱包认购, 活期申购, 活期认购, 活动奖励, 基金退款, 基金收益, 基金卖出, 普通定投, 钱包定投, 活期定投, 赎至银行卡, 强行赎回, 赎至钱包, 赎至活期, 快速取现, 用于买基金, 红利再投, 现金分红, 普通转换, 强行增加, 强行减少 |
| businessCode | 业务代码 | - |
| totalFee | 交易申请金额，精确到小数点后2位 | - |
| failFee | 交易失败金额，精确到小数点后2位 | - |
| totalFundUnits | 交易申请份额，小数位精度2 | - |
| failFundUnits | 交易失败份额，小数位精度2 | - |
| acceptTime | 交易发起时间，格式 yyyy-MM-dd HH:mm:ss | - |
| appSheetSerialNo | 交易申请单号 | - |
| transactionAccountId | 交易账号 | - |
| confirmFlag | 交易状态 | 0：待确认；1：已撤单；2：部分确认成功；3：确认成功；4：确认失败/认购接受失败；5：认购交易接受；6：订单作废 |
| payFlag | 支付状态 | 0：支付成功；1：支付失败；2：支付超时 |
| endFlag | 是否是最终状态 | 0：进行中状态；1：最终状态 |
| processStatus | 进行中状态 | 0：等待支付结果；1：预计XX-XX确认；2：交易失败 预计XX-XX回款；3：预计XX-XX回款；4：已撤单 预计XX-XX回款；5：待基金成立；6：进行中；7：提交申请中 |
| finalStatus | 最终状态 | 0：确认成功；1：成功；2：部分成功；3：部分成功 有退款；4：交易失败 有退款；5：已撤单；6：交易失败 |
| changeFundCode | 转换专用，转换的目标基金代码 | - |
| changeFundName | 转换专用，转换的目标基金名称 | - |
| fundCode | 基金代码，产品为个基时才有 | - |
| fundName | 基金名称，产品为个基时才有 | - |
| groupId | 组合id，产品为组合时才有 | - |
| groupName | 组合名称，产品为组合时才有 | - |
| status | 组合状态，产品为组合时才有，用于传递给组合交易记录详情页 | 0：组合子订单有未确认或作废的；1：组合已撤单 |
| toAccountTime | 预计回款时间，格式 yyyy-MM-dd HH:mm:ss | - |
| exceptConfirmTime | 预计确认时间，格式 yyyy-MM-dd HH:mm:ss | - |
| transactionCfmDate | 交易确认日，格式 yyyy-MM-dd | - |
| fixedIncomeFlag | 是否是固收 | 0：不是，1：是 |
| taCode | TA代码 | - |
| goldFlag | 是否是黄金宝交易 | - |
| goldConversionNum | 黄金宝转换系数，精确到小数点后两位 | - |
| ndConfirmedamount | 确认金额/转出确认金额 | - |
| ndConfirmedvol | 确认份额 | - |
| vcCanceltime | 撤单时间 | - |
| vcTransactioncfmdate | 确认日期 | - |
| vcCustid | 投资人id | - |
| vcSence | 业务场景 | GOLD（黄金宝），FIRMOFFER（实盘大赛） |

### 交易详情 data

| 字段 | 说明 |
|------|------|
| appSheetSerialNo | 订单号 |
| fundCode | 基金代码 |
| fundName | 基金名称 |
| businessCode | 业务代码 |
| businessName | 业务名称 |
| subBusinessTypeMsg | 子业务描述 |
| applicationAmount | 申请金额 |
| walletPayAmount | 钱包实际支付金额；存在时优先作为买入 `amount` |
| cardPayAmount | 银行卡实际支付金额；钱包支付金额为 0 时使用 |
| refundAmount | 认购部分确认或未确认时返还的资金 |
| actualFee | 实际费用 |
| confirmFlag | 确认状态 |
| cancelFlag | 撤单标志 (0可撤/1不可撤) |
| feeSource | 资金来源 (0银行卡/1活期/2钱包) |
| bankAccount | 银行卡尾号 |
| bankName | 银行名称 |
| acceptTime | 受理时间 |
| exceptCfmDate | 预计确认时间 |
| ftoAccountTime | 银行卡到账时间 |
| ftoscAccountTime | 钱包到账时间 |
| incomeDate | 收益日期 |
| targetFundCode / targetFundName | `redemption_for_buying` 对应的购买基金代码/名称 |

### 基金转换详情字段

基金转换记录必须查询详情，使用确认字段进行分析：

| 字段 | 说明 |
|------|------|
| outFundCode / outFundName | 转出基金代码/名称 |
| outConfirmedVol | 确认转出份额 |
| outNav | 转出确认净值 |
| inFundCode / inFundName | 转入基金代码/名称 |
| inConfirmedVol | 确认转入份额 |
| inNav | 转入确认净值 |
| charge | 转换费用 |
| navDate | 转换净值日期 |

### 列表字段补全规则

按以下规则使用 `appSheetSerialNo` 查询详情后再保存和分析：

- 买入、卖出记录的金额或份额缺少任意一个时，必须查询详情。
- `confirmFlag=1` 表示已撤单，`confirmFlag=4` 表示确认失败，`confirmFlag=6` 表示交易作废；这三类记录不需要补全数据，即使字段为空也不要查询详情。
- `subBusinessType` 包含 `reservation` 或描述包含“认购”时必须查询详情，即使列表金额和份额非空。
- 基金转换记录必须查询详情，以获取确认转出/转入份额和净值。
- `subBusinessType=redemption_for_buying` 时必须查询详情，以获取 `targetFundCode/targetFundName`。
- `amount`：优先读取详情的 `amount`，并兼容 `applicationAmount`、`confirmAmount` 等金额字段。
- 买入详情的 `walletPayAmount > 0` 时优先使用；否则使用 `cardPayAmount > 0`，再回退到申请金额。
- 认购详情存在 `refundAmount` 时，净支付金额和 FIFO 成本为 `实际支付金额 - refundAmount`，最低为 0。
- 费用可能有折扣或补贴，仅单独统计，不得再次叠加到 FIFO 成本。
- 保存并汇总 `refundAmount`，用于核对部分确认和全额返还记录。
- `shares`：优先读取详情的 `shares`，并兼容 `totalFundUnits`、`confirmShare` 等份额字段。
- 详情中的空值不得覆盖交易列表已有的非空字段。
- 每条详情查询成功后立即持久化，单条失败时保留列表数据，后续运行继续补全。

### 分红与基金转换分析规则

- `businessCode=098` 表示货币基金快速取现，按卖出处理；单位净值固定为 1，卖出金额等于卖出份额。
- `cash_dividend` 按现金分红金额计入已实现收益。
- `dividend_reinvestment` 按新增份额计入持仓，不增加现金投入成本。
- `firstBusinessType=other` 且 `shares > 0` 时，按份额分红/红利再投处理。
- 基金转换不是卖出：按 FIFO 扣减转出份额，将对应持仓成本迁移到 `inFundCode` 的 `inConfirmedVol`。
- 转换不确认买卖盈亏；目标基金后续卖出时按迁移后的单位成本计算。
- 转换详情不完整时暂不扣减转出份额，避免持仓成本丢失。
- `redemption_for_buying` 表示赎回货币基金用于购买目标基金；目标基金已有独立买入记录，不得合成额外买入。
- 使用相同 `acceptTime`、目标基金代码和金额匹配对应买入记录。
- 货币基金按真实赎回扣减 FIFO 份额，目标基金按真实买入记录建仓。
- 将配对的赎回与买入标记为内部资金划转，并从 `external_sell_amount`、`external_buy_amount` 中剔除。

### 单位净值与估值

```text
GET https://fund.10jqka.com.cn/quotation/fund_detail/v2/getNavData?fundCode=<fundCode>&range=now&type=unit
```

- 从响应 `data.unit[]` 读取 `date` 和 `value`。
- 分红日或估值日无净值时，向前取最近一个净值。
- 红利再投估算金额 = 新增份额 × 分红日单位净值。
- 剩余持仓市值 = 剩余份额 × 估值日单位净值。
- 未实现盈亏 = 剩余持仓市值 - 剩余成本。
- 估算总盈亏 = FIFO 已实现盈亏 + 现金分红 + 未实现盈亏；不要重复加入红利再投估算金额。

---

## 调用示例

### 查询交易列表首页

```bash
curl -X POST "https://trade.5ifund.com/openapi/order/v2/ai/orderlist" \
  -H "Authorization: Bearer <work_token>" \
  -H "Content-Type: application/json" \
  -d '{"custId":"CUST001","offset":1,"limit":200,"startDate":"20250101","endDate":"20250624","opBusinessCode":"all","opProductType":"all","queryProcessing":false}'
```

### 使用游标查询下一页

```bash
curl -X POST "https://trade.5ifund.com/openapi/order/v2/ai/orderlist" \
  -H "Authorization: Bearer <work_token>" \
  -H "Content-Type: application/json" \
  -d '{"custId":"CUST001","offset":2,"limit":200,"startDate":"20250101","endDate":"20250624","opBusinessCode":"all","opProductType":"all","queryProcessing":false,"lastAcceptTime":"2025-06-23 15:30:00","lastAppSheetSerialNo":"20250623000001"}'
```

### 查询交易详情

```bash
curl -X POST "https://trade.5ifund.com/openapi/order/v2/ai/detail" \
  -H "Authorization: Bearer <work_token>" \
  -H "Content-Type: application/json" \
  -d '{"appSheetSerialNo":"00000000000105785215"}'
```
