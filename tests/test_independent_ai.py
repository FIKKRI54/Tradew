import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from llm_advisors import (INDEPENDENT_CANDLE_LIMIT, _independent_prompt,
                          _validated_independent, independent_analysis,
                          independent_context)
from auto_execution import build_independent_execution_decision
from limit_ladder import attach_limit_ladder


def decision_fixture():
    bars = [{
        'time': index * 60, 'open': 100 + index, 'high': 101 + index,
        'low': 99 + index, 'close': 100.5 + index,
    } for index in range(30)]
    pivots = {
        'high': [{'price': 110 + index, 'time': index, 'confirmed_bar': index + 2}
                 for index in range(9)],
        'low': [{'price': 90 - index, 'time': index, 'confirmed_bar': index + 2}
                for index in range(9)],
    }
    active_supply = {
        'kind': 'SUPPLY', 'price': 125, 'low': 124, 'high': 126,
        'status': 'active', 'active': True, 'sources': ['H1'],
        'active_sources': ['H1'], 'broken_sources': [], 'unknown_sources': [],
    }
    # A genuine completed bullish engulfing at the independently chosen level.
    bars[-4].update(open=122.5, high=123, low=122, close=122.6)
    bars[-3].update(open=122.8, high=123, low=121, close=121.8)
    bars[-2].update(open=121.7, high=123.4, low=121, close=123.2)
    active_demand = {
        'kind': 'DEMAND', 'price': 121.8, 'low': 121.6, 'high': 122,
        'status': 'active', 'sources': ['M15'], 'active_sources': ['M15'],
    }
    return {
        'snapshot_id': 'blind-1', 'snapshot_time': 1000, 'symbol': 'XAUUSD.c',
        'session': 'London', 'bid': 123.4, 'ask': 123.6, 'spread': .2,
        'chart_data': {timeframe: [dict(bar) for bar in bars]
                       for timeframe in ('M1', 'M5', 'M15', 'M30', 'H1', 'H4', 'D1')},
        'structure': {timeframe: {'pivots': pivots, 'trend': 'HIDDEN_TREND'}
                      for timeframe in ('M1', 'M5', 'M15', 'M30', 'H1', 'H4', 'D1')},
        'chart_analysis': {
            'zones': [active_supply, active_demand],
            'active_target_zones': {'BUY': [active_supply], 'SELL': []},
        },
        'data_quality': [], 'action': 'SELL', 'setup_state': 'ACTIONABLE',
        'buy_setup_score': 1, 'sell_setup_score': 99,
        'reasons': ['HIDDEN_REASON'], 'next_trigger': 'HIDDEN_TRIGGER',
        'entry': 123, 'stop': 130, 'targets': [110],
    }


class IndependentAiTests(unittest.TestCase):
    def test_independent_sell_plan_layers_from_confirmation_to_deep_entry(self):
        plan = {
            'direction': 'SELL', 'pending_price': 4317.67, 'level': 4316.44,
            'stop': 4323.61, 'targets': [4299.17], 'risk_fraction': .5,
        }
        layered = attach_limit_ladder(plan, 4315.91, 4316.17, {})
        self.assertEqual([round(item['price'], 3) for item in layered['limit_layers']],
                         [4316.44, 4317.055, 4317.67])
        self.assertEqual([item['risk_weight'] for item in layered['limit_layers']],
                         [.25, .35, .4])
        self.assertTrue(all(item['target'] == 4299.17
                            for item in layered['limit_layers']))

    def test_equal_independent_level_uses_quote_to_build_distinct_sell_layers(self):
        plan = {
            'direction': 'SELL', 'pending_price': 4338.02, 'level': 4338.02,
            'stop': 4343.91, 'targets': [4330.3], 'risk_fraction': .5,
        }
        layered = attach_limit_ladder(plan, 4335.47, 4335.73, {})
        self.assertEqual([round(item['price'], 2) for item in layered['limit_layers']],
                         [4336.32, 4337.17, 4338.02])
        self.assertEqual([item['risk_weight'] for item in layered['limit_layers']],
                         [.25, .35, .4])

    def test_equal_independent_level_uses_quote_to_build_distinct_buy_layers(self):
        plan = {
            'direction': 'BUY', 'pending_price': 4333.92, 'level': 4333.92,
            'stop': 4325.12, 'targets': [4347.31], 'risk_fraction': .5,
        }
        layered = attach_limit_ladder(plan, 4338.94, 4339.2, {})
        self.assertEqual([round(item['price'], 2) for item in layered['limit_layers']],
                         [4337.44, 4335.68, 4333.92])
        self.assertEqual([item['risk_weight'] for item in layered['limit_layers']],
                         [.25, .35, .4])

    def test_independent_schema_uses_codex_supported_nullable_object(self):
        schema = json.loads(Path('llm_independent_schema.json').read_text(encoding='utf-8'))
        trade_plan = schema['properties']['trade_plan']
        self.assertEqual(trade_plan['type'], ['object', 'null'])
        self.assertNotIn('oneOf', trade_plan)
        self.assertFalse(trade_plan['additionalProperties'])
        self.assertEqual(set(trade_plan['required']), set(trade_plan['properties']))

    def test_blind_context_bounds_completed_candles_and_levels(self):
        decision = decision_fixture()
        context = independent_context(decision)
        self.assertEqual(len(context['candles']['M5']), INDEPENDENT_CANDLE_LIMIT)
        self.assertEqual(context['candle_fields'], ['time', 'open', 'high', 'low', 'close'])
        self.assertEqual(context['candles']['M5'][-1][0], decision['chart_data']['M5'][-2]['time'])
        self.assertNotIn(decision['chart_data']['M5'][-1]['time'],
                         [candle[0] for candle in context['candles']['M5']])
        self.assertEqual(len(context['confirmed_structural_levels']['H4']['high']), 6)
        self.assertEqual(len(context['confirmed_structural_levels']['D1']['high']), 6)
        self.assertEqual(context['active_target_zones']['BUY'][0]['source_timeframes'], ['H1'])
        self.assertEqual(context['active_target_zones']['BUY'][0]['high'], 126)
        serialized = json.dumps(context)
        for hidden in ('HIDDEN_REASON', 'HIDDEN_TRIGGER', 'HIDDEN_TREND',
                       'buy_setup_score', 'sell_setup_score', 'setup_state', 'targets'):
            self.assertNotIn(hidden, serialized)

    def test_independent_prompt_contains_only_blind_context_values(self):
        context = independent_context(decision_fixture())
        prompt = _independent_prompt(context)
        self.assertIn('BLIND_MARKET_CONTEXT', prompt)
        self.assertIn('blind_independent_market_read', prompt)
        self.assertNotIn('HIDDEN_REASON', prompt)
        self.assertNotIn('HIDDEN_TRIGGER', prompt)

    def test_blind_context_normalizes_broker_timestamps_to_utc(self):
        decision = decision_fixture()
        context = independent_context(decision, timestamp_offset_seconds=10800)
        self.assertEqual(context['candles']['M5'][-1][0],
                         decision['chart_data']['M5'][-2]['time'] - 10800)
        latest_level = decision['structure']['H4']['pivots']['high'][-1]
        normalized = context['confirmed_structural_levels']['H4']['high'][-1]
        self.assertEqual(normalized[1], latest_level['time'] - 10800)
        self.assertEqual(normalized[2], latest_level['confirmed_bar'] - 10800)
        self.assertIn('UTC', context['time_basis'])

    def test_independent_schema_validation(self):
        payload = {
            'market_bias': 'NEUTRAL', 'recommended_action': 'WAIT',
            'setup_status': 'WATCH', 'summary': 'Range remains unresolved.',
            'risk_tier': None, 'risk_reason': 'No executable setup exists.',
            'plain_language_summary': 'Price has no clear direction, so wait for a stronger move.',
            'evidence': ['M5 remains inside confirmed levels.'],
            'entry_condition': 'Wait for a completed breakout.',
            'invalidation_condition': 'A close back inside invalidates the breakout.',
            'objectives': ['Nearest confirmed opposing level.'], 'risk_flags': [],
            'trade_plan': None,
        }
        result = _validated_independent(payload, 'test', 'model', 1.2)
        self.assertEqual(result['recommended_action'], 'WAIT')
        with self.assertRaises(ValueError):
            _validated_independent({**payload, 'recommended_action': 'CLOSE'}, 'test', 'model', 1)

    def test_ready_independent_plan_is_structured_and_directionally_valid(self):
        payload = {
            'market_bias': 'BULLISH', 'recommended_action': 'BUY',
            'setup_status': 'READY', 'summary': 'Buy retest.',
            'risk_tier': 'MEDIUM',
            'risk_reason': 'The structure is clear but not exceptional.',
            'plain_language_summary': 'Price may pull back before moving higher.',
            'evidence': ['Completed candles support the plan.'],
            'entry_condition': 'Buy the planned lower price.',
            'invalidation_condition': 'Exit below support.',
            'objectives': ['Prior high.'], 'risk_flags': [],
            'trade_plan': {
                'direction': 'BUY', 'order_type': 'BUY_LIMIT',
                'entry_price': 122, 'stop_loss': 120, 'take_profit': 125,
                'confirmation_level': 121, 'structural_invalidation': 120,
                'expires_in_minutes': 15,
            },
        }
        self.assertEqual(_validated_independent(
            payload, 'test', 'model', 1)['trade_plan']['entry_price'], 122)
        with self.assertRaisesRegex(ValueError, 'explicit valid risk_tier'):
            _validated_independent({**payload, 'risk_tier': None}, 'test', 'model', 1)
        with self.assertRaises(ValueError):
            _validated_independent({**payload, 'trade_plan': {
                **payload['trade_plan'], 'order_type': 'SELL_LIMIT'}}, 'test', 'model', 1)

    def test_independent_execution_plan_does_not_use_deterministic_direction(self):
        source = decision_fixture()
        source['snapshot_time'] = time.time()
        source['positions_summary'] = {'positions': [], 'pending_orders': []}
        live = {**source, 'snapshot_id': 'live-2', 'action': 'SELL',
                'bid': 123.4, 'ask': 123.6, 'data_quality': [],
                'account_mode': 'demo'}
        config = json.loads(Path('tests/fixtures/scan_config.json').read_text(encoding='utf-8'))
        config['expected_mode'] = 'demo'
        analysis = {
            'snapshot_id': source['snapshot_id'],
            'analysis': {
                'status': 'ok', 'setup_status': 'READY', 'recommended_action': 'BUY',
                'risk_tier': 'MEDIUM',
                'risk_reason': 'The structure is clear but not exceptional.',
                'trade_plan': {
                    'direction': 'BUY', 'order_type': 'BUY_LIMIT',
                    'entry_price': 122, 'stop_loss': 120, 'take_profit': 125,
                    'confirmation_level': 121,
                    'structural_invalidation': 120, 'expires_in_minutes': 15,
                },
            },
        }
        planned = build_independent_execution_decision(source, live, analysis, config)
        self.assertEqual(planned['planned_direction'], 'BUY')
        self.assertEqual(planned['entry_plan']['source'], 'independent_llm')
        self.assertEqual(planned['entry_plan']['rule_version'], 'independent-auto-29')
        self.assertEqual(planned['entry_plan']['risk_tier'], 'MEDIUM')
        self.assertEqual(planned['entry_plan']['risk_fraction'], .2)
        self.assertEqual(planned['entry_plan']['pending_price'], 122)

    def test_independent_plan_allows_owned_runner_at_breakeven(self):
        source = decision_fixture()
        source['snapshot_time'] = time.time()
        config = json.loads(Path('tests/fixtures/scan_config.json').read_text(encoding='utf-8'))
        config['expected_mode'] = 'demo'
        runner = {
            'ticket': 7, 'type': 0, 'volume': .1, 'price_open': 110, 'sl': 110,
            'magic': config['execution_magic'],
            'comment': 'Tradew S:0123456789 L1R',
        }
        source['positions_summary'] = {
            'positions': [runner], 'pending_orders': []}
        live = {**source, 'snapshot_id': 'live-runner',
                'bid': 123.4, 'ask': 123.6, 'data_quality': [],
                'account_mode': 'demo'}
        analysis = {
            'snapshot_id': source['snapshot_id'],
            'analysis': {
                'status': 'ok', 'setup_status': 'READY',
                'recommended_action': 'BUY',
                'risk_tier': 'LOW',
                'risk_reason': 'Valid with meaningful uncertainty.',
                'trade_plan': {
                    'direction': 'BUY', 'order_type': 'BUY_LIMIT',
                    'entry_price': 122, 'stop_loss': 120,
                    'take_profit': 125, 'confirmation_level': 121,
                    'structural_invalidation': 120,
                    'expires_in_minutes': 15,
                },
            },
        }
        planned = build_independent_execution_decision(
            source, live, analysis, config)
        self.assertEqual(planned['planned_direction'], 'BUY')
        self.assertEqual(planned['entry_plan']['risk_fraction'], .1)

    def test_independent_execution_degrades_narrow_ladder_to_reduced_single_limit(self):
        source = decision_fixture()
        source['snapshot_time'] = time.time()
        source['positions_summary'] = {'positions': [], 'pending_orders': []}
        live = {**source, 'snapshot_id': 'live-2', 'action': 'SELL',
                'bid': 123.4, 'ask': 123.6, 'data_quality': [],
                'account_mode': 'demo'}
        config = json.loads(Path('tests/fixtures/scan_config.json').read_text(encoding='utf-8'))
        config['expected_mode'] = 'demo'
        analysis = {
            'snapshot_id': source['snapshot_id'],
            'analysis': {
                'status': 'ok', 'setup_status': 'READY', 'recommended_action': 'BUY',
                'risk_tier': 'HIGH',
                'risk_reason': 'Exceptionally aligned without material conflict.',
                'trade_plan': {
                    'direction': 'BUY', 'order_type': 'BUY_LIMIT',
                    'entry_price': 122, 'stop_loss': 120, 'take_profit': 125,
                    'confirmation_level': 121.95,
                    'structural_invalidation': 120, 'expires_in_minutes': 15,
                },
            },
        }
        planned = build_independent_execution_decision(source, live, analysis, config)
        plan = planned['entry_plan']
        self.assertEqual(plan['ladder_execution_profile'],
                         'single_limit_reduced_risk')
        self.assertEqual(len(plan['limit_layers']), 1)
        self.assertEqual(plan['limit_layers'][0]['price'], 122)
        self.assertEqual(plan['limit_layers'][0]['risk_weight'], .25)
        self.assertEqual(plan['risk_fraction'], .3)
        self.assertAlmostEqual(plan['limit_ladder_total_risk_fraction'], .075)
        self.assertIn('reduced-risk limit', planned['reasons'][0])

    def test_independent_execution_requires_source_and_live_active_opposing_zone(self):
        source = decision_fixture()
        source['snapshot_time'] = time.time()
        source['positions_summary'] = {'positions': [], 'pending_orders': []}
        live = {**source, 'snapshot_id': 'live-2', 'bid': 123.4, 'ask': 123.6,
                'data_quality': [], 'account_mode': 'demo'}
        live['chart_analysis'] = {
            'zones': [{**source['chart_analysis']['zones'][0], 'status': 'broken'}],
            'active_target_zones': {'BUY': [], 'SELL': []},
        }
        config = json.loads(Path('tests/fixtures/scan_config.json').read_text(encoding='utf-8'))
        config['expected_mode'] = 'demo'
        analysis = {
            'snapshot_id': source['snapshot_id'],
            'analysis': {
                'status': 'ok', 'setup_status': 'READY', 'recommended_action': 'BUY',
                'risk_tier': 'MEDIUM',
                'risk_reason': 'The structure is clear but not exceptional.',
                'trade_plan': {
                    'direction': 'BUY', 'order_type': 'BUY_LIMIT',
                    'entry_price': 122, 'stop_loss': 120, 'take_profit': 125,
                    'confirmation_level': 121,
                    'structural_invalidation': 120, 'expires_in_minutes': 15,
                },
            },
        }
        with self.assertRaisesRegex(ValueError, 'no longer active'):
            build_independent_execution_decision(source, live, analysis, config)

    def test_independent_execution_rejects_target_outside_active_zone(self):
        source = decision_fixture()
        source['snapshot_time'] = time.time()
        source['positions_summary'] = {'positions': [], 'pending_orders': []}
        live = {**source, 'snapshot_id': 'live-2', 'bid': 123.4, 'ask': 123.6,
                'data_quality': [], 'account_mode': 'demo'}
        config = json.loads(Path('tests/fixtures/scan_config.json').read_text(encoding='utf-8'))
        config['expected_mode'] = 'demo'
        analysis = {
            'snapshot_id': source['snapshot_id'],
            'analysis': {
                'status': 'ok', 'setup_status': 'READY', 'recommended_action': 'BUY',
                'risk_tier': 'MEDIUM',
                'risk_reason': 'The structure is clear but not exceptional.',
                'trade_plan': {
                    'direction': 'BUY', 'order_type': 'BUY_LIMIT',
                    'entry_price': 122, 'stop_loss': 120, 'take_profit': 127,
                    'confirmation_level': 121,
                    'structural_invalidation': 120, 'expires_in_minutes': 15,
                },
            },
        }
        with self.assertRaisesRegex(ValueError, 'source-snapshot active M5-H1 opposing zone'):
            build_independent_execution_decision(source, live, analysis, config)

    def test_independent_analysis_uses_selected_provider_and_separate_log(self):
        response = {
            'provider': 'gemini_vps', 'model': 'web', 'status': 'ok',
            'market_bias': 'NEUTRAL', 'recommended_action': 'WAIT',
            'setup_status': 'WATCH', 'summary': 'Wait.', 'evidence': [],
            'risk_tier': None, 'risk_reason': 'There is no executable setup.',
            'plain_language_summary': 'Price is unclear, so wait.',
            'entry_condition': 'Fresh M5 close.',
            'invalidation_condition': 'Return inside structure.',
            'objectives': [], 'risk_flags': [], 'trade_plan': None,
        }
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            root.joinpath('llm_config.json').write_text(
                Path('tests/fixtures/llm_config.json').read_text(encoding='utf-8'), encoding='utf-8')
            root.joinpath('scan_config.json').write_text(
                Path('tests/fixtures/scan_config.json').read_text(encoding='utf-8'), encoding='utf-8')
            with patch.dict('os.environ', {'TRADEW_DATA_ROOT': folder}), patch(
                    'llm_advisors.review_with_gemini', return_value=response) as gemini, patch(
                    'llm_advisors.review_with_codex') as astra:
                result = independent_analysis(decision_fixture(), 'gemini', root)
            gemini.assert_called_once()
            astra.assert_not_called()
            self.assertTrue(result['blind_input'])
            self.assertEqual(result['analysis']['recommended_action'], 'WAIT')
            logged = json.loads(root.joinpath('llm_independent_analyses.jsonl').read_text())
            self.assertNotIn('action', logged['context'])

    def test_independent_api_is_separate_from_cross_check(self):
        import main
        from api_server import app
        main.LAST = decision_fixture()
        result = {'snapshot_id': 'blind-1', 'blind_input': True,
                  'analysis': {'status': 'ok', 'recommended_action': 'WAIT'}}
        with patch('llm_advisors.independent_analysis', return_value=result) as analyze:
            response = app.test_client().post('/api/ai/independent', json={'provider': 'astra'})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()['blind_input'])
        self.assertEqual(analyze.call_args.args[1], 'astra')


if __name__ == '__main__':
    unittest.main()
