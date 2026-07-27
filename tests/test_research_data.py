import json
from datetime import datetime
from types import SimpleNamespace

from frontend import api_server
from src.analysis.entry_guard import EntryGuard
from src.news import news_fetcher as news_module


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def test_stock_news_uses_bare_code_and_normalizes_report_symbols(monkeypatch):
    calls = []

    def fake_em_get(url, params=None, **_kwargs):
        calls.append((url, dict(params or {})))
        if 'security/ann' in url:
            return _Response({
                'data': {
                    'list': [{
                        'title_ch': '年度报告',
                        'codes': [{'stock_code': '600000'}],
                        'columns': [{'column_name': '定期报告'}],
                        'art_code': 'AN1',
                        'notice_date': '2026-07-23',
                    }]
                }
            })
        return _Response({
            'data': [{
                'title': '公司研究',
                'stockName': '浦发银行',
                'stockCode': '600000',
                'publishDate': '2026-07-22',
                'infoCode': 'R1',
            }]
        })

    monkeypatch.setattr(news_module, 'em_get', fake_em_get)
    result = news_module.NewsFetcher().fetch_stock_news_with_meta('sh600000', count=5)

    assert any(params.get('stock_list') == '600000' for _, params in calls)
    assert any(params.get('code') == '600000' for _, params in calls)
    assert {item.symbols[0] for item in result['items']} == {'sh600000'}
    assert result['status']['available'] is True


def test_research_coverage_does_not_treat_no_recent_news_as_fetch_failure():
    result = EntryGuard().build_research_snapshot(
        news_items=[],
        fundamentals={'pe_ttm': 12.0},
        capital_flow={
            'total_main_net': 1_000_000,
            'last_main_net': 100_000,
            'source': '东方财富近20日资金流',
        },
        market_regime={'code': 'neutral', 'score': 55, 'label': '震荡'},
        source_status={'news': {'available': True, 'records': 0}},
        average_amount=100_000_000,
    )

    assert result['available']['news'] is True
    assert result['completeness'] == 1.0
    assert result['missing'] == []
    assert '近期未检出' in result['summaries']['news']


def test_entry_guard_names_missing_research_instead_of_generic_condition():
    guard = EntryGuard()
    research = guard.build_research_snapshot(
        news_items=[],
        fundamentals={},
        capital_flow={},
        market_regime={'code': 'neutral', 'score': 55, 'label': '震荡'},
        source_status={'news': {'available': False}},
    )
    result = guard.evaluate(
        'sh600000',
        {'price': 10, 'pre_close': 9.9, 'open': 9.95, 'high': 10.1, 'low': 9.8},
        {
            'action': 'buy', 'suggested_qty': 100,
            'buy_low': 9.5, 'buy_high': 10.5,
            'stop_loss': 9.0, 'target_price': 12.0,
        },
        {'samples': 8, 'win_rate': 60, 'avg_return': 2.0},
        research=research,
        reference_price=10,
        market_open=True,
    )

    assert result['status'] == 'research_incomplete'
    assert result['label'] == '研究数据未取全，暂不下单'
    assert result['missing_data'] == ['公告/研报', '财务指标', '资金流']
    assert '当前缺少' in result['reasons'][-1]


def test_pick_status_preserves_specific_original_veto(monkeypatch):
    generated_at = datetime.now().isoformat()
    research = EntryGuard().build_research_snapshot(
        news_items=[],
        fundamentals={'pe_ttm': 12},
        capital_flow={'total_main_net': 100_000, 'last_main_net': 10_000},
        market_regime={'code': 'neutral', 'score': 55, 'label': '震荡'},
        source_status={'news': {'available': True}},
        average_amount=100_000_000,
    )
    stored = {
        'symbol': 'sh600000',
        'action': 'hold',
        'approval_label': '历史样本验证未通过',
        'approval_failures': ['历史验证未通过：样本 2 次'],
        'analysis_price': 10,
        'generated_at': generated_at,
        'planned_qty': 100,
        'entry_plan': {
            'action': 'buy', 'suggested_qty': 100,
            'buy_low': 9.5, 'buy_high': 10.5,
            'stop_loss': 9, 'target_price': 12,
        },
        'validation': {'samples': 8, 'win_rate': 60, 'avg_return': 2},
        'research': research,
    }
    monkeypatch.setattr(
        api_server.realtime,
        'get_quotes',
        lambda *args, **kwargs: {
            'sh600000': {
                'price': 10, 'pre_close': 9.9, 'open': 9.95,
                'high': 10.1, 'low': 9.8, 'change_pct': 1.01,
            }
        },
    )
    monkeypatch.setattr(api_server, 'market_session', lambda now: SimpleNamespace(is_open=True))
    original = dict(api_server._scan_job)
    api_server._scan_job.update({
        'status': 'done', 'pool': 'market', 'total': 1, 'done': 1,
        'current': '', 'candidates': 40, 'picks': [stored],
        'started_at': generated_at, 'finished_at': generated_at, 'error': '',
    })
    try:
        payload = json.loads(api_server.ai_pick_status().body.decode('utf-8'))
    finally:
        api_server._scan_job.clear()
        api_server._scan_job.update(original)

    guard = payload['picks'][0]['entry_guard']
    assert guard['label'] == '历史样本验证未通过'
    assert guard['reasons'] == ['历史验证未通过：样本 2 次']
