"""
爱基金交易客户端 — 项目内直接完成基金实盘交易, 不依赖 WorkBuddy。

封装同花顺爱基金开放平台的 HTTP API, 凭证由 aijijin-sdk 管理
(先执行 `init(INIT_TOKEN)` 持久化到 ~/.aijijin/credentials.json,
交易时自动换取 Work Token)。

支持能力:
  - 基金持仓查询 (含钱包持仓)
  - 申购全流程: 初始化 → 费用 → 协议 → 签约检查 → 下单 → 订单详情
  - 赎回: 初始化 → 下单 → 订单详情
  - 交易记录查询
  - 撤单

参考: aijijin-bundle/skills/fund-*/ 文档 (API 契约来源)
"""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import requests

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

BASE_URL = "https://trade.5ifund.com/openapi"
TOHANGQING_URL = "https://trade.5ifund.com/tohangqing"
FUND_FEE_URL = "https://fund.10jqka.com.cn/interface/fund/tradeRule"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}


class FundTraderError(Exception):
    """基金交易异常基类。"""


class FundNotInitializedError(FundTraderError):
    """aijijin-sdk 尚未初始化 (未执行 init)。"""


class FundTokenExpiredError(FundTraderError):
    """Work Token 失效且刷新失败 (Refresh Token 过期)。"""


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------

class FundTrader:
    """爱基金交易客户端。

    用法:
        trader = FundTrader()
        holdings = trader.get_holdings()
        trader.buy("000001", 1000, pay_type="wallet")  # 金额申购
        trader.redeem("000001", 500.0, trans_account_id="...")
    """

    def __init__(self, timeout: float = 15.0, max_token_retry: int = 2):
        self.timeout = timeout
        self.max_token_retry = max_token_retry
        self._work_token: Optional[str] = None
        self._token_ts: float = 0.0
        self._session = requests.Session()
        self._session.headers.update(HEADERS)

    # ------------------------------------------------------------------
    # Token 管理
    # ------------------------------------------------------------------

    def _get_work_token(self) -> str:
        """获取 Work Token (带进程内 5 分钟缓存)。"""
        now = time.time()
        if self._work_token and (now - self._token_ts) < 240:
            return self._work_token
        try:
            from aijijin_sdk import get_work_token
            self._work_token = get_work_token()
        except Exception as e:  # noqa: BLE001
            name = type(e).__name__
            if "CredentialsNotFound" in name or "not initialized" in str(e).lower():
                raise FundNotInitializedError(
                    "爱基金凭证未初始化: 请先执行\n"
                    "  D:/py/python.exe -c \"from aijijin_sdk import init; init('你的INIT_TOKEN')\"\n"
                    "INIT_TOKEN 在同花顺 App → 理财 → 基金 Skill 页面获取。"
                ) from e
            if "RefreshTokenExpired" in name:
                raise FundTokenExpiredError(
                    "Refresh Token 已过期: 请去同花顺 App 重新获取 INIT_TOKEN 并执行 init()"
                ) from e
            raise FundTraderError(f"获取 Work Token 失败: {e}") from e
        self._token_ts = now
        return self._work_token

    def _auth_headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self._get_work_token()}"}

    # ------------------------------------------------------------------
    # HTTP 请求 (带 token 失效自动刷新)
    # ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        base: str = BASE_URL,
        params: Optional[Dict] = None,
        json_body: Optional[Dict] = None,
        data: Optional[Dict] = None,
    ) -> Dict:
        """发送请求, 401 时自动换新 token 重试。返回解析后的 JSON dict。"""
        url = f"{base.rstrip('/')}/{path.lstrip('/')}"
        last_exc: Optional[Exception] = None

        for attempt in range(self.max_token_retry + 1):
            headers = self._auth_headers()
            try:
                resp = self._session.request(
                    method, url, params=params,
                    json=json_body, data=data,
                    headers=headers, timeout=self.timeout,
                )
            except requests.RequestException as e:
                raise FundTraderError(f"网络请求失败 [{method} {path}]: {e}") from e

            if resp.status_code == 401 and attempt < self.max_token_retry:
                # token 失效 → 强制刷新重试
                self._work_token = None
                self._token_ts = 0.0
                continue

            if resp.status_code >= 400:
                raise FundTraderError(
                    f"爱基金接口错误 [{resp.status_code}] {path}: {resp.text[:300]}"
                )

            try:
                return resp.json()
            except json.JSONDecodeError:
                raise FundTraderError(f"爱基金返回非 JSON: {resp.text[:300]}") from None

        raise FundTraderError(f"token 刷新后仍失败: {last_exc}")

    def _check_ok(self, data: Dict, action: str) -> None:
        """检查业务状态码。status_code == '0000' 表示成功。"""
        code = data.get("status_code") or data.get("statusCode") or ""
        if code and str(code) != "0000":
            msg = (
                data.get("status_msg")
                or data.get("message")
                or (data.get("error") or {}).get("msg")
                or f"未知错误(code={code})"
            )
            raise FundTraderError(f"{action}失败: {msg}")
        err = data.get("error") or {}
        if err.get("id") not in (None, 0):
            raise FundTraderError(f"{action}失败: {err.get('msg', err)}")

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def get_holdings(self, share_category: str = "01") -> Dict:
        """基金持仓列表。

        Args:
            share_category: 份额类别 01=普通基金, 02=养老, ... 07=全查
        """
        data = self._request(
            "POST", "/holdposition/v2/ai/list",
            json_body={"shareCategory": share_category},
        )
        self._check_ok(data, "查询持仓")
        return data.get("data", {})

    def get_wallet_home(self) -> Dict:
        """钱包首页 (钱包持仓/余额)。"""
        data = self._request("GET", "/v1/query_wallet_home")
        self._check_ok(data, "查询钱包")
        return data.get("data", {})

    def get_all_holdings(self) -> Dict:
        """查询全部类别持仓并汇总。"""
        merged: Dict[str, Any] = {
            "fundList": [], "wallet": None,
            "totalValue": 0.0, "totalIncome": 0.0,
        }
        for cat in ("01", "02", "03", "04", "05", "06", "07"):
            try:
                h = self.get_holdings(cat)
            except FundNotInitializedError:
                raise
            except FundTraderError:
                continue
            rows = h.get("fundPositonDetailList") or h.get("fundList") or []
            for r in rows:
                if r.get("combineFlag") == 1 or not any(
                    x.get("fundCode") == r.get("fundCode") for x in merged["fundList"]
                ):
                    merged["fundList"].append(r)
        try:
            merged["wallet"] = self.get_wallet_home()
        except FundNotInitializedError:
            raise
        except FundTraderError:
            pass
        return merged

    def get_order_list(
        self,
        cust_id: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: int = 20,
        op_business_code: str = "all",
        offset: int = 1,
    ) -> List[Dict]:
        """交易记录列表。

        Args:
            cust_id: 客户 ID (申购初始化返回的 data.custId)
            start_date/end_date: yyyyMMdd, 默认最近 30 天
        """
        if not start_date:
            start_date = (datetime.now() - timedelta(days=30)).strftime("%Y%m%d")
        if not end_date:
            end_date = datetime.now().strftime("%Y%m%d")
        body = {
            "custId": cust_id,
            "offset": offset,
            "limit": limit,
            "startDate": start_date,
            "endDate": end_date,
            "opBusinessCode": op_business_code,
            "opProductType": "all",
            "queryProcessing": False,
        }
        data = self._request("POST", "/order/v2/ai/orderlist", json_body=body)
        self._check_ok(data, "查询交易记录")
        result = data.get("data", {})
        if isinstance(result, dict):
            return result.get("list") or result.get("records") or []
        return []

    def get_order_detail(self, app_sheet_serial_no: str) -> Dict:
        """订单详情。"""
        data = self._request(
            "POST", "/order/v2/ai/detail",
            json_body={"appSheetSerialNo": app_sheet_serial_no},
        )
        self._check_ok(data, "查询订单详情")
        return data.get("data", {})

    # ------------------------------------------------------------------
    # 申购
    # ------------------------------------------------------------------

    def subscribe_init(self, fund_code: str) -> Dict:
        """申购初始化: 返回基金信息/风险等级/账户列表等。"""
        data = self._request(
            "GET", "/ai/subscribe/init",
            params={"fundCode": fund_code},
        )
        self._check_ok(data, "申购初始化")
        return data.get("data", {})

    def get_fund_fee(self, fund_code: str) -> Dict:
        """基金费用信息 (管理费/托管费/申购费率/赎回费率)。无需 token。"""
        url = f"{FUND_FEE_URL}/{fund_code}"
        try:
            resp = self._session.get(url, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json().get("data", {})
        except Exception as e:
            raise FundTraderError(f"查询基金费用失败: {e}") from e

    def get_agreements(self, fund_code: str) -> List[Dict]:
        """查询申购需确认的协议列表。"""
        url = f"{TOHANGQING_URL}/interface/fund/detail/{fund_code}_tradeInitTreaty"
        try:
            resp = self._session.get(url, headers=self._auth_headers(), timeout=self.timeout)
            resp.raise_for_status()
            d = resp.json()
        except Exception as e:
            raise FundTraderError(f"查询协议失败: {e}") from e
        data = d.get("data", {})
        if isinstance(data, dict):
            return data.get("tradeInitTreaty") or []
        return []

    def record_agreement_read(self, cust_id: str, agreements: List[Dict]) -> None:
        """记录协议已阅读 (下单前必须调用)。"""
        data = self._request(
            "POST", "/record/trade",
            json_body={"custId": cust_id, "agreements": agreements, "sourceType": "BUY"},
        )
        self._check_ok(data, "记录协议阅读")

    def check_contract(
        self, fund_code: str, amount: float, trans_account_id: str
    ) -> Dict:
        """签约检查。"""
        data = self._request(
            "POST", "/ai/sign_contract/v1/check_before_trade",
            json_body={
                "applicationAmount": str(amount),
                "fundCode": fund_code,
                "transactionAccountId": trans_account_id,
            },
        )
        self._check_ok(data, "签约检查")
        return data.get("data", {})

    def buy(
        self,
        fund_code: str,
        amount: float,
        *,
        pay_type: str = "wallet",
        trans_account_id: str = "",
        cust_id: str = "",
        confirm_agreements: bool = True,
    ) -> Dict:
        """基金申购 (金额)。

        Args:
            fund_code: 基金代码
            amount: 申购金额 (元)
            pay_type: 'wallet'=钱包(buyType=1), 'bank'=银行卡(buyType=0)
            trans_account_id: 交易账户 ID (从 subscribe_init 的账户列表取;
                不传时自动选第一个)
            cust_id: 客户 ID (协议阅读记录用; 不传则从 init 返回取)
            confirm_agreements: 是否自动记录协议已阅读 (默认 True)
        """
        init_data = self.subscribe_init(fund_code)
        if not cust_id:
            cust_id = init_data.get("custId", "")

        # 账户列表
        if not trans_account_id:
            trans_account_id = self._pick_account(init_data, pay_type)

        # 协议
        agreements = self.get_agreements(fund_code)
        if confirm_agreements and agreements and cust_id:
            self.record_agreement_read(cust_id, agreements)

        # 签约检查
        try:
            self.check_contract(fund_code, amount, trans_account_id)
        except FundTraderError:
            pass  # 钱包足额时跳过; 银行卡场景可能失败, 不阻塞下单

        buy_type = "1" if pay_type == "wallet" else "0"
        data = self._request(
            "POST", "/ai/buy",
            data={
                "buyType": buy_type,
                "fundCode": fund_code,
                "money": str(amount),
                "transactionAccountId": trans_account_id,
            },
        )
        self._check_ok(data, "基金申购")
        result = data.get("data", {})
        # 补订单详情
        serial = result.get("appSheetSerialNo") or result.get("appsheetserialno")
        detail: Dict = {}
        if serial:
            try:
                detail = self.get_order_detail(str(serial))
            except FundTraderError:
                pass
        return {
            "appSheetSerialNo": serial,
            "fundCode": fund_code,
            "amount": amount,
            "payType": pay_type,
            "buyType": buy_type,
            "transactionAccountId": trans_account_id,
            "detail": detail,
            "raw": result,
        }

    def _pick_account(self, init_data: Dict, pay_type: str) -> str:
        """从 init 数据中挑选默认交易账户。"""
        if pay_type == "wallet":
            wallet_list = init_data.get("fundtzeroList") or []
            if wallet_list:
                acc = wallet_list[0]
                return str(
                    acc.get("transactionAccountId")
                    or acc.get("transActionAccountId")
                    or acc.get("accountId")
                    or ""
                )
        # bankCardSplitListResult 本身即 list (实测结构, 非 {"list": [...]} 包装)
        bank_list = init_data.get("bankCardSplitListResult") or []
        if isinstance(bank_list, dict):
            bank_list = bank_list.get("list") or []
        if not bank_list:
            bank_list = init_data.get("bankCardList") or []
        if bank_list:
            acc = bank_list[0]
            return str(
                acc.get("transactionAccountId")
                or acc.get("transActionAccountId")
                or acc.get("accountId")
                or ""
            )
        raise FundTraderError(
            "未找到可用交易账户, 请手动传入 trans_account_id"
        )

    # ------------------------------------------------------------------
    # 赎回
    # ------------------------------------------------------------------

    def redeem_render(self, fund_code: str, trans_account_id: str) -> Dict:
        """赎回初始化: 返回净值/可赎份额/费率档位。"""
        data = self._request(
            "POST", "/ai/redemption/v1/render",
            json_body={
                "transactionAccountId": trans_account_id,
                "fundCode": fund_code,
            },
        )
        self._check_ok(data, "赎回初始化")
        return data.get("data", {})

    def redeem(
        self,
        fund_code: str,
        share_vol: float,
        trans_account_id: str,
        *,
        redemption_type: str = "1",
    ) -> Dict:
        """基金赎回 (份额)。

        Args:
            fund_code: 基金代码
            share_vol: 赎回份额
            trans_account_id: 交易账户 ID
            redemption_type: '1'=钱包赎回(优先), '0'=银行卡赎回
        """
        data = self._request(
            "POST", "/ai/redemption/v2/redeem",
            data={
                "fundCode": fund_code,
                "redemptionType": redemption_type,
                "shareVol": str(share_vol),
                "transActionAccountId": trans_account_id,
            },
        )
        self._check_ok(data, "基金赎回")
        result = data.get("data", {})
        serial = result.get("appSheetSerialNo") or result.get("appsheetserialno")
        detail: Dict = {}
        if serial:
            try:
                detail = self.get_order_detail(str(serial))
            except FundTraderError:
                pass
        return {
            "appSheetSerialNo": serial,
            "fundCode": fund_code,
            "shareVol": share_vol,
            "redemptionType": redemption_type,
            "detail": detail,
            "raw": result,
        }

    # ------------------------------------------------------------------
    # 撤单
    # ------------------------------------------------------------------

    def revoke(self, app_sheet_serial_no: str) -> Dict:
        """撤单 (申购/赎回均可)。"""
        data = self._request(
            "POST", "/ai/revoke",
            json_body={
                "operator": "999",
                "revokeAppSheetNo": app_sheet_serial_no,
            },
        )
        self._check_ok(data, "撤单")
        return data.get("data", {})

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    @staticmethod
    def md5(text: str) -> str:
        return hashlib.md5(text.encode()).hexdigest()


def format_holdings(holdings: Dict) -> str:
    """把持仓 dict 格式化为可读文本。"""
    lines: List[str] = []
    fund_rows = holdings.get("fundList") or []
    total_value = 0.0
    total_income = 0.0
    for r in fund_rows:
        name = r.get("fundName", "")
        code = r.get("fundCode", "")
        vol = r.get("holdVol", r.get("totalAmount", 0))
        value = r.get("totalAmount", 0) or 0
        income = r.get("holdIncome", 0) or 0
        rate = r.get("holdIncomeRate", "")
        total_value += float(value or 0)
        total_income += float(income or 0)
        lines.append(
            f"  {name}({code}) 份额={vol} 市值={value}元 "
            f"收益={income}元({rate}%)"
        )
    wallet = holdings.get("wallet") or {}
    wallet_value = 0.0
    if isinstance(wallet, dict):
        wallet_value = float(
            wallet.get("totalValue") or wallet.get("sumValue") or 0
        )
    lines.insert(0, f"基金持仓 ({len(fund_rows)} 只, 市值 {total_value:.2f} 元, 收益 {total_income:.2f} 元)")
    if wallet_value:
        lines.append(f"  钱包资产: {wallet_value:.2f} 元")
    return "\n".join(lines)
