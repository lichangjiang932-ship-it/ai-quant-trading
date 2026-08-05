# 基金申购展示模板

本文件维护 `fund-purchase` 的用户可见展示格式。输出要简洁、稳定、适合终端阅读；优先使用 Markdown 表格和短段落，避免超宽 ASCII 分隔线。

## 通用排版规则

- 对结构化信息使用 Markdown 表格。
- 每个阶段使用二级或三级标题，不使用超长等号分隔线。
- 金额统一展示为 `¥金额` 或 `金额 元`，同一张表内保持一致。
- 账号只展示银行名和尾号，不展示完整卡号。
- 风险提示、协议确认、签约提醒等需要用户确认的节点，最后一行必须明确告诉用户可回复的内容。
- 不展示底层状态字段名和值，例如 `confirmFlag`、`checkFlag`、`failMsg.code`。

## 1. 基金申购信息

```markdown
## 基金申购信息

| 项目 | 内容 |
|---|---|
| 基金代码 | <fundCode> |
| 基金名称 | <fundName> |
| 申购金额 | <amount> 元 |
| 起购金额 | <minBuy> 元 |
| 追加金额 | <minAddBuy> 元 |
| 单笔最大购买 | <maxBuy> 元 |
| 产品风险等级 | R<fundRiskLevel> |
| 客户风险等级 | C<ov_clientriskrate> |

### 阶梯费率

| 申购金额区间 | 原始费率 |
|---|---:|
| <money1> | <rate1> |
| <money2> | <rate2> |

### 购买折扣

| 支付方式 | 折扣 |
|---|---|
| 银行卡 | <银行卡折扣描述> |
| 钱包 | <钱包折扣描述> |

### 持有期间费用

| 费用类型 | 费率 |
|---|---:|
| 管理费 | <glf> |
| 托管费 | <tgf> |
| 销售服务费 | <fwf> |
```

如有封闭期，在持有期间费用后追加：

```markdown
### 封闭期提示

⚠️ <封闭期提示文案>
```

## 2. 风险二次确认

```markdown
## 风险提示

该产品「<fundName>（<fundCode>）」的风险等级（R<fundRiskLevel>）高于您自身的风险等级（C<ov_clientriskrate>）。

购买该产品/服务，可能导致您承担超出自身承受能力的损失和不利后果。请您认真了解产品风险特征，慎重购买。

如需继续本次申购，请回复：`确认继续`  
如不继续，请回复：`不继续`
```

## 3. 支付方式选择

### 钱包 + 银行卡

```markdown
## 请选择支付方式

### 钱包账户

| 编号 | 账户 | 可用余额 |
|---|---|---:|
| 钱包1 | <bankName>-<tailNo> | ¥<availableVol> |
| 钱包2 | <bankName>-<tailNo> | ¥<availableVol> |

### 银行卡

| 编号 | 账户 | 单笔限额 | 单日限额 |
|---|---|---:|---:|
| 银行卡1 | <bankName>-<tailNo> | ¥<maxPurchaseOfOne> | ¥<maxPurchaseOfDay> |
| 银行卡2 | <bankName>-<tailNo> | ¥<maxPurchaseOfOne> | ¥<maxPurchaseOfDay> |

请回复编号选择支付方式，例如：`钱包1` 或 `银行卡1`。
```

### 仅银行卡

```markdown
## 请选择支付方式

### 银行卡

| 编号 | 账户 | 单笔限额 | 单日限额 |
|---|---|---:|---:|
| 银行卡1 | <bankName>-<tailNo> | ¥<maxPurchaseOfOne> | ¥<maxPurchaseOfDay> |
| 银行卡2 | <bankName>-<tailNo> | ¥<maxPurchaseOfOne> | ¥<maxPurchaseOfDay> |

请回复编号选择支付方式，例如：`银行卡1`。
```

## 4. 协议确认

```markdown
## 协议确认

请仔细阅读、了解并确认以下协议：

- [<协议1名称>](<协议1URL>)
- [<协议2名称>](<协议2URL>)
- [<协议3名称>](<协议3URL>)

<高风险产品附加文案，如适用：且已阅读产品详情页，知晓本产品为高风险。>

您可以点击上述链接查看协议详细内容。

如已阅读并同意继续交易，请回复：`已阅读`
```

模板中不得放示例真实 URL，实际输出只能使用本轮协议查询接口返回并解析后的 URL。

## 5. 签约提醒

```markdown
## 签约提醒

该银行卡可能需要升级签约。

如需升级，请前往 App「个人中心」→「银行卡」→ 选择对应银行 → 去升级。

你也可以忽略该提示并继续本次交易。如确认继续，请回复：`y`
```

## 6. 申购结果：订单成功

```markdown
## 基金申购结果

| 项目 | 内容 |
|---|---|
| 基金代码 | <fundCode> |
| 基金名称 | <fundName> |
| 购买金额 | <amount> 元 |
| 支付方式 | <钱包/银行卡> - <bankName>-<tailNo> |
| 申请下单时间 | <acceptTime> |
| 交易状态 | ✅ 订单成功 |
| 预计确认时间 | <exceptCfmDate> |
| 预计可查看收益时间 | <incomeDate> |
| 订单号 | <appSheetSerialNo> |
```

## 7. 申购结果：处理中

```markdown
## 基金申购结果

| 项目 | 内容 |
|---|---|
| 申请下单时间 | <acceptTime 或 -> |
| 交易状态 | ⏳ 订单处理中 |
| 提示 | 请稍后重新查询结果 |
| 订单号 | <appSheetSerialNo> |
```

## 8. 申购结果：失败

```markdown
## 基金申购结果

| 项目 | 内容 |
|---|---|
| 申请下单时间 | <acceptTime 或 -> |
| 交易状态 | ❌ 订单失败 |
| 失败原因 | <failMsg.thsMessage 或 failMsg.message> |
| 订单号 | <appSheetSerialNo> |
```

## 9. 申购结果：已撤单

```markdown
## 基金申购结果

| 项目 | 内容 |
|---|---|
| 申请下单时间 | <acceptTime 或 -> |
| 交易状态 | ↩️ 已撤单 |
| 订单号 | <appSheetSerialNo> |
```

## 10. 申购结果：基金公司确认失败

```markdown
## 基金申购结果

| 项目 | 内容 |
|---|---|
| 申请下单时间 | <acceptTime 或 -> |
| 交易状态 | ❌ 基金公司确认失败 |
| 订单号 | <appSheetSerialNo> |
```

## 11. 申购结果：部分确认成功，部分失败

```markdown
## 基金申购结果

| 项目 | 内容 |
|---|---|
| 申请下单时间 | <acceptTime 或 -> |
| 交易状态 | ⚠️ 基金公司部分确认成功，部分失败 |
| 已确认金额 | <confirmedAmount 或 -> |
| 已确认份额 | <confirmedVol 或 -> |
| 订单号 | <appSheetSerialNo> |
```