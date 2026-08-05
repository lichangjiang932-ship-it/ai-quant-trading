---
name: fund-holding-list
version: 0.1.6
description: 合并查询并展示用户的基金持仓与钱包持仓。用户询问“我的持仓、全部持仓、总持仓、资产持仓、查看全部资产、查看总资产、基金+钱包持仓、现在持有哪些资产、持仓列表、账户里有哪些基金/钱包资产”等总览类持仓/资产查询时，应使用此 skill，即使用户只说“查一下我的持仓”也要触发；它会先获取实时 work token，再同时尝试查询钱包首页 API 和基金持仓列表 API，并按固定规则合并、汇总、排序、打码后用 Markdown 表格输出。
---

# 持仓查询（fund-holding-list）

用于生成“基金持仓 + 钱包持仓”的总览结果。执行时要以实时接口数据为准，按本文档固定规则完成查询、合并、汇总和展示。

本 Skill 重点解决两类容易出错的问题：

- 数据口径：基金持仓来自多个 shareCategory，同一基金可能出现多条记录，必须合并后再汇总。
- 展示口径：基金名称较长且中英文混排，使用 Markdown 表格比空格对齐更稳定。

---

## 1. 严格执行要求

只要命中本 Skill 的使用场景，无论用户使用什么模型、客户端或表达方式，都要按本文档执行。不要自行简化流程、跳过接口、复用历史数据，或调整展示结构。

1. 先获取 work token，再调用接口；不要用历史 token、缓存数据或记忆数据替代实时查询。
2. 同时尝试查询两个数据来源：钱包首页 API、基金持仓列表 API。即使其中一个接口失败，也要继续尝试另一个接口。
3. 基金持仓按 shareCategory `01`、`02`、`03`、`04`、`05`、`06`、`07` 依次查询；同一基金出现多条记录时，优先使用接口提供的合并基金记录作为最终展示记录。
4. 严格按本文档的合并记录选择规则、汇总计算规则、排序规则、敏感信息打码规则和待确认份额规则处理数据。
5. 严格按“展示格式”输出对应内容；不要自行改成其他展示结构，也不要遗漏要求展示的字段。
6. 钱包持仓不要展示本文档未要求的收益率字段，例如七日年化收益率、万份收益率等。
7. 基金持仓展示区的总金额、持有收益、日收益，必须根据最终展示的基金持仓列表重新计算；其中总金额使用最终展示明细的 `totalAmount` 汇总，不要直接使用接口汇总字段。
8. 基金持仓明细中的总金额、持有收益、持有收益率、持有份额、日收益必须直接取最终展示记录的 `totalAmount`、`holdIncome`、`holdIncomeRate`、`holdVol`、`newestIncome`；同一基金出现多条记录时，如果接口已提供合并基金记录，优先使用该合并记录，不要自行累加明细记录计算这些展示字段。
9. 如果接口返回字段、数据口径或展示结果存在不一致，以本文档规则为准，并在必要时向用户说明差异。

### 执行前检查清单

在输出最终结果前，快速自检以下事项，避免遗漏关键步骤：

- 已重新获取 work token，而不是复用旧 token。
- 已尝试调用钱包首页 API。
- 已尝试调用基金持仓列表 API 的 7 个 shareCategory：`01`、`02`、`03`、`04`、`05`、`06`、`07`。
- 已调用 `scripts/aggregate_holdings.py` 执行累加、最终展示记录选择、汇总、排序、榜单计算。
- 基金总金额、持有收益、日收益来自脚本输出的最终展示基金列表汇总。
- 基金持仓明细字段直接使用脚本输出，未在模型回复中自行累加或覆盖 `totalAmount`、`holdIncome`、`holdIncomeRate`、`holdVol`、`newestIncome`。
- 基金持仓明细已按脚本输出顺序展示，即按总金额（`totalAmount`）从大到小排序。
- “最赚钱的5只基金”只包含 `holdIncome>0` 的基金；没有正收益基金时已省略整个榜单。
- “最亏损的5只基金”只包含 `holdIncome<0` 的基金；没有负收益基金时已省略整个榜单。
- 银行卡号已打码。
- 输出使用 Markdown 表格，未使用空格对齐长表格。

---

## 2. 执行流程总览

按以下顺序执行：

1. 获取 work token。
2. 查询钱包首页 API。
3. 按 shareCategory `01`~`07` 依次查询基金持仓列表 API。
4. 将钱包 API 和 7 个基金持仓 API 的原始响应整理为 JSON。
5. 调用 `scripts/aggregate_holdings.py` 执行所有累加、最终展示记录选择、汇总、排序、最赚钱/最亏损榜单计算。
6. 根据脚本输出对银行卡号等敏感信息打码展示。
7. 按“展示格式”输出钱包持仓、基金持仓、最赚钱 5 只基金、最亏损 5 只基金。

---

## 2.1 累加计算脚本（强制使用）

本 Skill 涉及累加计算、基金最终展示记录选择、基金汇总、排序、最赚钱/最亏损榜单时，必须调用：

```bash
python scripts/aggregate_holdings.py --input <raw_api_response.json> --output <aggregated_result.json>
```

输入 JSON 结构：

```json
{
  "wallet": "钱包首页 API 原始响应或包装对象",
  "funds": {
    "01": "shareCategory=01 的基金持仓 API 原始响应或包装对象",
    "02": "shareCategory=02 的基金持仓 API 原始响应或包装对象",
    "03": "shareCategory=03 的基金持仓 API 原始响应或包装对象",
    "04": "shareCategory=04 的基金持仓 API 原始响应或包装对象",
    "05": "shareCategory=05 的基金持仓 API 原始响应或包装对象",
    "06": "shareCategory=06 的基金持仓 API 原始响应或包装对象",
    "07": "shareCategory=07 的基金持仓 API 原始响应或包装对象"
  }
}
```

脚本负责：

- 钱包银行卡明细金额累加。
- 同一基金多条记录时，优先选择接口提供的合并基金记录作为最终展示记录。
- 基于最终展示基金列表累加基金总金额、持有收益、日收益。
- 基金持仓按 `totalAmount` 从大到小排序。
- 最赚钱 5 只基金、最亏损 5 只基金排序。
- 金额、收益率、待确认份额等展示字段格式化。

禁止在模型回复中手工重新实现上述累加逻辑；如果脚本执行失败，必须说明脚本失败原因，不得自行改用另一套计算规则。

---

## 3. 前置 Work Token 获取

在调用本 Skill 的任何功能前，必须先通过 aijijin-sdk 获取 work token。

### 获取 Token 命令

```bash
python -c "from aijijin_sdk import get_work_token; print(get_work_token())"
```

### Token 处理逻辑

1. 执行上述命令获取 work token。
2. 如果获取失败（返回空或报错），提示用户：`请检查 aijijin-sdk 配置是否正确`。
3. 每次执行本 Skill 都要重新获取 token，因为 token 有效期较短。

---

## 4. API 调用规范

本 Skill 需要调用两个 API。

| 接口 | URL | 方法 | 用途 |
|------|-----|------|------|
| 钱包首页 | `https://trade.5ifund.com/openapi/v1/query_wallet_home` | GET | 获取钱包持仓 |
| 基金持仓 | `https://trade.5ifund.com/openapi/holdposition/v2/ai/list` | POST | 获取基金持仓 |

认证方式：`Authorization: Bearer <work_token>`

### 4.1 查询钱包首页

```bash
curl -X GET "https://trade.5ifund.com/openapi/v1/query_wallet_home" \
  -H "Authorization: Bearer <work_token>"
```

### 4.2 查询基金持仓列表

依次查询 shareCategory 为 `01`、`02`、`03`、`04`、`05`、`06`、`07` 的持仓数据，然后合并展示。

```bash
for category in 01 02 03 04 05 06 07; do
  curl -X POST "https://trade.5ifund.com/openapi/holdposition/v2/ai/list" \
    -H "Authorization: Bearer <work_token>" \
    -H "Content-Type: application/json" \
    -d "{\"shareCategory\": \"$category\"}"
done
```

---

## 5. 响应字段说明

### 5.1 钱包首页字段

| 字段 | 说明 |
|------|------|
| `custId` | 客户号 |
| `yesterdayIncome` | 昨日日期（如 `07-21`，表示 7 月 21 日） |
| `profits` | 昨日收益金额，单位：元 |
| `holdProfits` | 持有收益，累计 |
| `fundCode` | 基金代码 |
| `fundName` | 基金名称 |
| `avaiableVol` | 可用份额 |
| `freezeMoney` | 冻结份额 |
| `convertFreezeMoney` | 转换冻结份额 |
| `usableCashOutVol` | 可用可取份额 |
| `usableUnCashOutVol` | 可用不可取份额 |
| `buyStatus` | 转入状态（`1`=可转入，`0`=不可转入） |

### 5.2 银行卡份额列表字段（bankAccountShareList）

| 字段 | 说明 |
|------|------|
| `bankName` | 银行名称 |
| `bankAccount` | 银行卡号，需打码展示，保留前 4 位 + 后 4 位 |
| `totalShare` | 该卡下钱包总份额 |

### 5.3 基金持仓字段（最终展示记录使用）

| 字段 | 说明 |
|------|------|
| `sumValue` | 接口返回总市值，单位：元；仅作参考 |
| `sumBuyAmount` | 接口返回总成本，单位：元；仅作参考，不用于最终展示汇总 |
| `sumHoldIncome` | 接口返回持有收益，单位：元；仅作参考，不用于最终展示汇总 |
| `sumNewestIncome` | 接口返回日收益，单位：元；仅作参考，不用于最终展示汇总 |
| `sumAccumulatedIncome` | 接口返回累计收益，单位：元；仅作参考 |
| `fundCode` | 基金代码 |
| `fundName` | 基金名称 |
| `shareValue` | 持仓市值，单位：元；仅作参考，不用于“总金额”展示 |
| `totalAmount` | 总金额/持有成本，单位：元；基金持仓顶部“总金额”和明细“总金额”都使用最终展示记录的该字段 |
| `holdVol` | 持有份额；明细展示直接使用最终展示记录的该字段 |
| `newestIncome` | 日收益，单位：元；明细展示直接使用最终展示记录的该字段 |
| `holdIncome` | 持有收益，单位：元；明细展示直接使用最终展示记录的该字段 |
| `holdIncomeRate` | 持有收益率；明细展示直接使用最终展示记录的该字段，不自行计算覆盖 |

---

## 6. 数据处理规则

### 6.1 基金最终展示记录选择

1. 从 7 个 shareCategory 响应中取出 `fundPositonCombinedList`，合并到候选数组。
2. 同一 `fundCode` 在不同 shareCategory 或不同 `transactionAccountId` 下存在多条记录时，优先使用接口提供的合并基金记录作为最终展示记录。
3. 最终展示记录一旦确定，明细中的总金额、持有收益、持有收益率、持有份额、日收益必须直接取该记录的 `totalAmount`、`holdIncome`、`holdIncomeRate`、`holdVol`、`newestIncome`。
4. 不要自行累加多条明细记录来覆盖上述字段；这些字段以最终展示记录为准。
5. 如果接口没有提供可识别的合并基金记录，才允许按 `fundCode` 进行兜底合并；兜底合并时只用于生成最终展示记录。

### 6.2 基金汇总计算

基金持仓展示区的汇总值必须根据最终展示基金列表重新计算：

- 总金额 = Σ 最终展示记录的 `totalAmount`
- 持有收益 = Σ 最终展示记录的 `holdIncome`
- 日收益 = Σ 最终展示记录的 `newestIncome`

不要直接使用接口返回的 `sumBuyAmount`、`sumHoldIncome`、`sumNewestIncome` 作为最终展示汇总。如果接口汇总字段与最终展示列表计算结果不一致，以最终展示列表计算结果为准。

### 6.3 持有收益率

- 基金持仓明细中的持有收益率直接使用最终展示记录的 `holdIncomeRate`。
- 不要自行用 `holdIncome / totalAmount` 计算后覆盖接口提供的 `holdIncomeRate`。
- 只有最终展示记录完全缺失 `holdIncomeRate` 时，才可以展示为空或 `-`。

### 6.4 排序、打码和特殊状态

1. 基金持仓按总金额（`totalAmount`）从大到小排序展示。
2. “最赚钱的5只基金”仅从持有收益 `holdIncome>0` 的基金中按持有收益从高到低选取，最多展示 5 只；如果没有任何正收益基金，则不展示该标题和表格。持有收益为 0 的基金不进入该榜单。
3. “最亏损的5只基金”仅从持有收益 `holdIncome<0` 的基金中按持有收益从低到高选取，最多展示 5 只；如果没有任何负收益基金，则不展示该标题和表格。持有收益为 0 的基金不进入该榜单。
4. 银行卡号打码展示，保留前 4 位 + 后 4 位，格式示例：`1234****5678`。
5. 基金持仓中 `holdVol=0` 且 `totalAmount>0` 的，显示为“待确认”状态。
6. 钱包的 `walletFinancialList`（理财通列表）不展示给用户。

---

## 7. 展示格式

按以下结构输出。字段为空时保留结构，并用接口实际可用值或合理空值填充；不要额外展示钱包收益率字段。

优先使用 Markdown 表格展示明细，避免中文基金名称过长时导致空格对齐错乱。数值列保持两位小数；基金持仓按总金额（`totalAmount`）从大到小排列。

输出时直接渲染 Markdown，不要把整段结果包进代码块；这样表格会被客户端正确渲染，用户更容易阅读。

```markdown
# 我的总持仓（基金 + 钱包）

## 钱包持仓

| 基金代码 | 基金名称 | 总金额 | 昨日收益 | 累计收益 |
|---|---|---:|---:|---:|
| <fundCode> | <fundName> | <avaiableVol> | <profits> | <holdProfits> |

- 昨日日期: <yesterdayIncome>
- 冻结份额: <freezeMoney>
- 转换冻结份额: <convertFreezeMoney>
- 可用可取份额: <usableCashOutVol>
- 可用不可取份额: <usableUnCashOutVol>
- 转入状态: <buyStatus>（1=可转入，0=不可转入）

### 钱包持仓明细

| 银行名称 | 卡号 | 持有金额 |
|---|---|---:|
| <bankName> | <maskedBankAccount> | <totalShare> |

## 基金持仓

- 总金额: <calculatedTotalAmount> 元
- 持有收益: <calculatedTotalHoldIncome> 元
- 日收益: <calculatedTotalNewestIncome> 元

| 基金代码 | 基金名称 | 总金额 | 持有收益 | 持有收益率 | 持有份额 | 日收益 |
|---|---|---:|---:|---:|---:|---:|
| <fundCode> | <fundName> | <totalAmount> | <holdIncome> | <holdIncomeRate> | <holdVol 或 待确认> | <newestIncome> |

### 最赚钱的5只基金

| 基金代码 | 基金名称 | 持有收益 | 持有收益率 |
|---|---|---:|---:|
| <topProfitFundCode1> | <topProfitFundName1> | <topProfitIncome1> | <topProfitRate1> |
| <topProfitFundCode2> | <topProfitFundName2> | <topProfitIncome2> | <topProfitRate2> |
| <topProfitFundCode3> | <topProfitFundName3> | <topProfitIncome3> | <topProfitRate3> |
| <topProfitFundCode4> | <topProfitFundName4> | <topProfitIncome4> | <topProfitRate4> |
| <topProfitFundCode5> | <topProfitFundName5> | <topProfitIncome5> | <topProfitRate5> |

### 最亏损的5只基金

| 基金代码 | 基金名称 | 持有收益 | 持有收益率 |
|---|---|---:|---:|
| <topLossFundCode1> | <topLossFundName1> | <topLossIncome1> | <topLossRate1> |
| <topLossFundCode2> | <topLossFundName2> | <topLossIncome2> | <topLossRate2> |
| <topLossFundCode3> | <topLossFundName3> | <topLossIncome3> | <topLossRate3> |
| <topLossFundCode4> | <topLossFundName4> | <topLossIncome4> | <topLossRate4> |
| <topLossFundCode5> | <topLossFundName5> | <topLossIncome5> | <topLossRate5> |
```

### 展示细节

- 不要使用依赖空格宽度对齐的长文本表格；中文、英文和数字混排时容易错位。
- 银行卡号按打码后的 `<maskedBankAccount>` 展示；如果接口只返回后 4 位，则展示为 `****<末4位>`。
- 基金名称不要为了对齐而截断；Markdown 表格允许长名称自然换行。
- 基金持仓中 `holdVol=0` 且 `totalAmount>0` 的，持有份额列展示为“待确认”。

---

## 8. 错误处理

| 场景 | 处理方式 |
|------|----------|
| 钱包 API 失败 | 单独展示基金持仓，并提示：`钱包数据获取失败` |
| 基金 API 失败 | 单独展示钱包持仓，并提示：`基金数据获取失败` |
| 两个 API 都失败 | 返回错误提示，请用户检查 token 配置 |
| token 获取失败 | 提示：`请检查 aijijin-sdk 配置是否正确` |

即使某个接口失败，也要保留已成功获取的数据展示，不要因为部分失败而丢弃全部结果。