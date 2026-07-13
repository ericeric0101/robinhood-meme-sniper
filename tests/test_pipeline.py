import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rh_meme_sniper.extract.ca_parser import extract_contract_addresses, extract_urls
from rh_meme_sniper.sources.apify_x import normalize_tweet_item, build_search_input, tweet_record
from rh_meme_sniper.sources.dexscreener import normalize_pair_item
from rh_meme_sniper.cluster.narrative_cluster import cluster_events
from rh_meme_sniper.score.authenticity import score_clusters
from rh_meme_sniper.alerts.telegram import render_candidate_alert
from rh_meme_sniper.cli import app
from rh_meme_sniper.pipeline import filter_pair_items, run_alert_loop_from_payload
from rh_meme_sniper.sources.apify_client import ApifyRunResult, ApifyClient
from rh_meme_sniper.sources.x_source import get_x_provider, TwitterAPIIOProvider
from rh_meme_sniper.models import CandidateToken, NarrativeCluster, RawEvent
from rh_meme_sniper.alerts.telegram import render_candidate_alert

from typer.testing import CliRunner

runner = CliRunner()


def test_extract_contract_addresses_and_urls():
    text = (
        'Real Robinhood Office Cat CA 0x1111111111111111111111111111111111111111 '
        'mirror 0x2222222222222222222222222222222222222222 '
        'https://dexscreener.com/robinhood/0xabc?maker=1'
    )

    assert extract_contract_addresses(text) == [
        '0x1111111111111111111111111111111111111111',
        '0x2222222222222222222222222222222222222222',
    ]
    assert extract_urls(text) == ['https://dexscreener.com/robinhood/0xabc?maker=1']


def test_normalize_apify_tweet_item_extracts_core_fields():
    item = {
        'id': 'tweet-1',
        'text': 'Robinhood Office Cat is live 0x1111111111111111111111111111111111111111',
        'url': 'https://x.com/some/status/1',
        'createdAt': '2026-07-09T01:18:00Z',
        'author': {'userName': 'kol_alpha', 'name': 'KOL Alpha'},
        'likeCount': 100,
        'retweetCount': 12,
    }

    event = normalize_tweet_item(item)

    assert event.source == 'x'
    assert event.source_id == 'tweet-1'
    assert event.author_handle == 'kol_alpha'
    assert event.contract_addresses == ['0x1111111111111111111111111111111111111111']
    assert event.urls == ['https://x.com/some/status/1']
    assert event.metrics['like_count'] == 100


def test_build_search_input_uses_query_and_limits():
    payload = build_search_input(query='robinhood office cat', max_items=25)
    assert payload['searchTerms'] == ['robinhood office cat']
    assert payload['maxItems'] == 25
    assert payload['sort'] == 'Top'
    assert payload['includeSearchTerms'] is True


def test_build_search_input_maps_latest_plus_top_for_xtdata():
    payload = build_search_input(
        query='robinhood office cat',
        max_items=10,
        sort='Latest + Top',
        actor_id='xtdata/twitter-x-scraper',
    )

    assert payload['sort'] == 'Both'


def test_build_search_input_keeps_original_sort_for_non_xtdata_actor():
    payload = build_search_input(
        query='robinhood office cat',
        max_items=10,
        sort='Latest + Top',
        actor_id='apidojo/tweet-scraper',
    )

    assert payload['sort'] == 'Latest + Top'
    assert 'includeSearchTerms' not in payload


def test_normalize_dxtdata_tweet_item_handles_full_text_and_screen_name():
    item = {
        'id': 'tweet-xt-1',
        'full_text': 'Robinhood Office Cat launches 0x1111111111111111111111111111111111111111',
        'url': 'https://x.com/some/status/xt1',
        'twitterUrl': 'https://twitter.com/some/status/xt1',
        'created_at': 'Tue Jun 16 19:39:42 +0000 2026',
        'author': {'screen_name': 'nasa_like'},
        'favorite_count': 250,
        'retweet_count': 30,
        'reply_count': 12,
        'quote_count': 8,
        'searchTerms': 'from:NASA lang:en',
    }

    event = normalize_tweet_item(item)
    record = tweet_record(item)

    assert event.author_handle == 'nasa_like'
    assert event.text is not None
    assert event.text.startswith('Robinhood Office Cat launches')
    assert event.metrics['engagement_total'] == 300
    assert record['author'] == 'nasa_like'
    assert record['text'] == item['full_text']
    assert record['searchTerms'] == 'from:NASA lang:en'
    assert record['createdAt'] == '2026-06-16T19:39:42Z'


def test_normalize_dex_pair_item_extracts_market_metrics():
    item = {
        'pairAddress': 'pair-1',
        'chainId': 'robinhood',
        'baseToken': {
            'address': '0x1111111111111111111111111111111111111111',
            'name': 'Robinhood Office Cat',
            'symbol': 'ROC',
        },
        'url': 'https://dexscreener.com/robinhood/pair-1',
        'pairCreatedAt': 1783559940000,
        'liquidity': {'usd': 15000},
        'volume': {'h24': 120000, 'h1': 12000},
        'txns': {'h1': {'buys': 120, 'sells': 80}},
    }

    event = normalize_pair_item(item)

    assert event.source == 'dexscreener'
    assert event.contract_addresses == ['0x1111111111111111111111111111111111111111']
    assert event.names == ['Robinhood Office Cat']
    assert event.symbols == ['ROC']
    assert event.metrics['liquidity_usd'] == 15000
    assert event.metrics['buy_count_1h'] == 120


def test_cluster_and_score_pipeline_prefers_canonical_earlier_ca():
    real_ca = '0x1111111111111111111111111111111111111111'
    fake_ca = '0x2222222222222222222222222222222222222222'

    apify_events = [
        normalize_tweet_item({
            'id': 't1',
            'text': f'Robinhood Office Cat CA {real_ca}',
            'createdAt': '2026-07-09T01:18:00Z',
            'author': {'userName': 'kol_alpha'},
            'url': 'https://x.com/a/status/1',
            'likeCount': 200,
            'retweetCount': 25,
        }),
        normalize_tweet_item({
            'id': 't2',
            'text': f'Office Cat CTO {fake_ca}',
            'createdAt': '2026-07-09T01:25:00Z',
            'author': {'userName': 'random_copycat'},
            'url': 'https://x.com/b/status/2',
        }),
    ]
    dex_events = [
        normalize_pair_item({
            'pairAddress': 'pair-real',
            'chainId': 'robinhood',
            'baseToken': {'address': real_ca, 'name': 'Robinhood Office Cat', 'symbol': 'ROC'},
            'url': 'https://dexscreener.com/robinhood/pair-real',
            'pairCreatedAt': 1783560000000,
            'liquidity': {'usd': 25000},
            'volume': {'h24': 200000, 'h1': 25000},
            'txns': {'h1': {'buys': 200, 'sells': 160}},
        }),
        normalize_pair_item({
            'pairAddress': 'pair-fake',
            'chainId': 'robinhood',
            'baseToken': {'address': fake_ca, 'name': 'Office Cat CTO', 'symbol': 'OFC'},
            'url': 'https://dexscreener.com/robinhood/pair-fake',
            'pairCreatedAt': 1783560600000,
            'liquidity': {'usd': 4000},
            'volume': {'h24': 6000, 'h1': 500},
            'txns': {'h1': {'buys': 15, 'sells': 20}},
        }),
    ]

    clusters = cluster_events(apify_events + dex_events)
    scored = score_clusters(clusters)

    assert len(scored) == 1
    cluster = scored[0]
    assert cluster.canonical_candidate is not None
    assert cluster.canonical_candidate.contract_address == real_ca
    assert cluster.canonical_candidate.authenticity_score > cluster.candidates[1].authenticity_score
    assert cluster.canonical_candidate.verdict == 'alert'


def test_render_candidate_alert_includes_scores_and_contract_address():
    real_ca = '0x1111111111111111111111111111111111111111'
    cluster = score_clusters(cluster_events([
        normalize_tweet_item({
            'id': 't1',
            'text': f'Robinhood Office Cat CA {real_ca}',
            'createdAt': '2026-07-09T01:18:00Z',
            'author': {'userName': 'kol_alpha'},
            'url': 'https://x.com/a/status/1',
            'likeCount': 200,
            'retweetCount': 25,
        }),
        normalize_pair_item({
            'pairAddress': 'pair-real',
            'chainId': 'robinhood',
            'baseToken': {'address': real_ca, 'name': 'Robinhood Office Cat', 'symbol': 'ROC'},
            'url': 'https://dexscreener.com/robinhood/pair-real',
            'pairCreatedAt': 1783560000000,
            'liquidity': {'usd': 25000},
            'volume': {'h24': 200000, 'h1': 25000},
            'txns': {'h1': {'buys': 200, 'sells': 160}},
        }),
    ]))[0]

    text = render_candidate_alert(cluster.canonical_candidate)

    assert 'Robinhood Office Cat' in text
    assert real_ca in text
    assert 'authenticity' in text.lower()
    assert 'alert' in text.lower()


def test_cli_analyze_sample_outputs_alert(tmp_path):
    real_ca = '0x1111111111111111111111111111111111111111'
    payload = {
        'tweets': [
            {
                'id': 't1',
                'text': f'Robinhood Office Cat CA {real_ca}',
                'createdAt': '2026-07-09T01:18:00Z',
                'author': {'userName': 'kol_alpha'},
                'url': 'https://x.com/a/status/1',
                'likeCount': 200,
                'retweetCount': 25,
            }
        ],
        'pairs': [
            {
                'pairAddress': 'pair-real',
                'chainId': 'robinhood',
                'baseToken': {'address': real_ca, 'name': 'Robinhood Office Cat', 'symbol': 'ROC'},
                'url': 'https://dexscreener.com/robinhood/pair-real',
                'pairCreatedAt': 1783560000000,
                'liquidity': {'usd': 25000},
                'volume': {'h24': 200000, 'h1': 25000},
                'txns': {'h1': {'buys': 200, 'sells': 160}},
            }
        ],
    }
    sample = tmp_path / 'sample.json'
    sample.write_text(__import__('json').dumps(payload))

    result = runner.invoke(app, ['analyze-sample', str(sample)])

    assert result.exit_code == 0
    assert 'Robinhood Office Cat' in result.stdout
    assert real_ca in result.stdout


def test_apify_free_plan_detection_matches_demo_mode_items_and_upsell_message():
    result = ApifyRunResult(
        run_id='run-1',
        status='SUCCEEDED',
        status_message='Your run has finished. Please subscribe to a paid plan on Apify for the best experience.',
        dataset_id='dataset-1',
        items=[{'noResults': True} for _ in range(10)],
    )

    assert ApifyClient._is_free_plan_api_block(result) is True


def test_apify_demo_items_with_upsell_message_are_treated_as_access_block():
    result = ApifyRunResult(
        run_id='run-demo',
        status='SUCCEEDED',
        status_message='Your run has finished. Please subscribe to a paid plan on Apify for the best experience.',
        dataset_id='dataset-demo',
        items=[{'demo': True} for _ in range(10)],
    )

    assert ApifyClient._is_free_plan_api_block(result) is True


def test_apify_no_results_without_paid_plan_message_is_not_treated_as_access_block():
    result = ApifyRunResult(
        run_id='run-2',
        status='SUCCEEDED',
        status_message='Your run has finished.',
        dataset_id='dataset-2',
        items=[{'noResults': True}],
    )

    assert ApifyClient._is_free_plan_api_block(result) is False


def test_get_x_provider_defaults_to_apify(monkeypatch):
    monkeypatch.setenv('X_PROVIDER', 'apify')

    provider = get_x_provider()

    assert provider.provider_name == 'apify'


def test_get_x_provider_selects_twitterapiio(monkeypatch):
    monkeypatch.setenv('X_PROVIDER', 'twitterapiio')

    provider = get_x_provider()

    assert provider.provider_name == 'twitterapiio'


def test_get_x_provider_rejects_unknown_provider(monkeypatch):
    monkeypatch.setenv('X_PROVIDER', 'unknown-provider')

    with __import__('pytest').raises(ValueError, match='Unsupported X provider'):
        get_x_provider()


def test_run_live_alert_loop_uses_selected_provider(monkeypatch, tmp_path):
    from rh_meme_sniper.pipeline import run_live_alert_loop

    real_ca = '0x1111111111111111111111111111111111111111'
    provider_calls = []

    class FakeProvider:
        provider_name = 'twitterapiio'

        def search_tweets(self, *, query, max_items, sort, tweet_language):
            provider_calls.append({
                'query': query,
                'max_items': max_items,
                'sort': sort,
                'tweet_language': tweet_language,
            })
            return [
                {
                    'id': 'tweet-1',
                    'text': f'Robinhood Office Cat CA {real_ca}',
                    'createdAt': '2026-07-09T01:18:00Z',
                    'author': {'userName': 'kol_alpha'},
                    'url': 'https://x.com/a/status/1',
                    'likeCount': 200,
                    'retweetCount': 25,
                }
            ]

    monkeypatch.setattr('rh_meme_sniper.pipeline.get_x_provider', lambda provider_name=None, actor_id=None: FakeProvider())
    monkeypatch.setattr('rh_meme_sniper.pipeline._fetch_pairs', lambda query_terms, chain_id=None, limit_per_query=5: [
        {
            'pairAddress': 'pair-real',
            'chainId': 'robinhood',
            'baseToken': {'address': real_ca, 'name': 'Robinhood Office Cat', 'symbol': 'ROC'},
            'url': 'https://dexscreener.com/robinhood/pair-real',
            'pairCreatedAt': 1783560000000,
            'liquidity': {'usd': 25000},
            'volume': {'h24': 200000, 'h1': 25000},
            'txns': {'h1': {'buys': 200, 'sells': 160}},
        }
    ])

    artifacts = run_live_alert_loop(
        query='robinhood office cat',
        max_items=5,
        sort='Latest',
        output_dir=tmp_path,
        send_telegram=False,
    )

    assert provider_calls == [{
        'query': 'robinhood office cat',
        'max_items': 5,
        'sort': 'Latest',
        'tweet_language': 'en',
    }]
    assert artifacts.tweet_count == 1
    assert artifacts.alert_count == 1


def test_run_live_alert_loop_requires_tweet_match_for_account_probe(monkeypatch, tmp_path):
    from rh_meme_sniper.pipeline import run_live_alert_loop

    real_ca = '0x020bfC650A365f8BB26819deAAbF3E21291018b4'

    class FakeProvider:
        provider_name = 'twitterapiio'

        def search_tweets(self, *, query, max_items, sort, tweet_language):
            return []

    monkeypatch.setattr('rh_meme_sniper.pipeline.get_x_provider', lambda provider_name=None, actor_id=None: FakeProvider())
    monkeypatch.setattr('rh_meme_sniper.pipeline._fetch_pairs', lambda query_terms, chain_id=None, limit_per_query=5: [
        {
            'pairAddress': 'pair-real',
            'chainId': 'robinhood',
            'baseToken': {'address': real_ca, 'name': 'Cash Cat', 'symbol': 'CASHCAT'},
            'url': 'https://dexscreener.com/robinhood/pair-real',
            'pairCreatedAt': 1783560000000,
            'liquidity': {'usd': 25000},
            'volume': {'h24': 200000, 'h1': 25000},
            'txns': {'h1': {'buys': 200, 'sells': 160}},
        }
    ])

    artifacts = run_live_alert_loop(
        query='from:vladtenev cashcat',
        max_items=5,
        sort='Latest',
        output_dir=tmp_path,
        send_telegram=False,
        require_tweet_match_for_alerts=True,
    )

    assert artifacts.tweet_count == 0
    assert artifacts.pair_count == 0
    assert artifacts.alert_count == 0


def test_filter_recent_tweets_accepts_twitterapiio_timeline_datetime():
    from rh_meme_sniper.pipeline import _filter_recent_tweets

    tweets = [
        {
            'id': 'tweet-new',
            'createdAt': 'Mon Jul 13 13:52:39 +0000 2026',
            'text': 'Robinhood Chain update',
        }
    ]
    now = datetime(2026, 7, 13, 14, 0, tzinfo=timezone.utc)

    assert _filter_recent_tweets(tweets, max_tweet_age_days=14, now=now) == tweets


def test_run_live_alert_loop_filters_stale_tweets_for_account_probe(monkeypatch, tmp_path):
    from rh_meme_sniper.pipeline import run_live_alert_loop

    real_ca = '0xD7321801CAae694090694Ff55A9323139F043B88'

    class FakeProvider:
        provider_name = 'twitterapiio'

        def search_tweets(self, *, query, max_items, sort, tweet_language):
            return [
                {
                    'id': 'tweet-old',
                    'text': 'the juggernaut',
                    'createdAt': '2025-05-24T07:04:00Z',
                    'author': {'userName': 'vladtenev'},
                    'url': 'https://x.com/vladtenev/status/1',
                    'likeCount': 1,
                    'retweetCount': 0,
                }
            ]

    monkeypatch.setattr('rh_meme_sniper.pipeline.get_x_provider', lambda provider_name=None, actor_id=None: FakeProvider())
    monkeypatch.setattr('rh_meme_sniper.pipeline._fetch_pairs', lambda query_terms, chain_id=None, limit_per_query=5: [
        {
            'pairAddress': 'pair-real',
            'chainId': 'robinhood',
            'baseToken': {'address': real_ca, 'name': 'The Juggernaut', 'symbol': 'JUGGERNAUT'},
            'url': 'https://dexscreener.com/robinhood/pair-real',
            'pairCreatedAt': 1783560000000,
            'liquidity': {'usd': 25000},
            'volume': {'h24': 200000, 'h1': 25000},
            'txns': {'h1': {'buys': 200, 'sells': 160}},
        }
    ])

    artifacts = run_live_alert_loop(
        query='from:vladtenev juggernaut',
        max_items=5,
        sort='Latest',
        output_dir=tmp_path,
        send_telegram=False,
        require_tweet_match_for_alerts=True,
        max_tweet_age_days=30,
    )

    assert artifacts.tweet_count == 0
    assert artifacts.pair_count == 0
    assert artifacts.alert_count == 0


def test_twitterapiio_provider_uses_advanced_search_and_paginates(monkeypatch):
    calls = []

    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    payloads = [
        {
            'tweets': [
                {'id': '1', 'text': 'one', 'author': {'userName': 'a'}, 'createdAt': 'Tue Dec 10 07:00:30 +0000 2024', 'url': 'https://x.com/a/status/1'},
                {'id': '2', 'text': 'two', 'author': {'userName': 'b'}, 'createdAt': 'Tue Dec 10 07:00:31 +0000 2024', 'url': 'https://x.com/b/status/2'},
            ],
            'has_next_page': True,
            'next_cursor': 'cursor-2',
        },
        {
            'tweets': [
                {'id': '3', 'text': 'three', 'author': {'userName': 'c'}, 'createdAt': 'Tue Dec 10 07:00:32 +0000 2024', 'url': 'https://x.com/c/status/3'},
                {'id': '4', 'text': 'four', 'author': {'userName': 'd'}, 'createdAt': 'Tue Dec 10 07:00:33 +0000 2024', 'url': 'https://x.com/d/status/4'},
            ],
            'has_next_page': False,
            'next_cursor': '',
        },
    ]

    def fake_get(url, *, params=None, headers=None, timeout=None):
        calls.append({'url': url, 'params': params, 'headers': headers, 'timeout': timeout})
        return FakeResponse(payloads[len(calls) - 1])

    monkeypatch.setattr('rh_meme_sniper.sources.x_source.httpx.get', fake_get)
    provider = TwitterAPIIOProvider(api_key='test-key')

    tweets = provider.search_tweets(query='cashcat robinhood', max_items=3, sort='Top', tweet_language='en')

    assert len(tweets) == 3
    assert [tweet['id'] for tweet in tweets] == ['1', '2', '3']
    assert calls[0]['url'] == 'https://api.twitterapi.io/twitter/tweet/advanced_search'
    assert calls[0]['params'] == {'query': 'cashcat robinhood', 'queryType': 'Top'}
    assert calls[0]['headers']['X-API-Key'] == 'test-key'
    assert calls[1]['params'] == {'query': 'cashcat robinhood', 'queryType': 'Top', 'cursor': 'cursor-2'}


def test_twitterapiio_provider_fetches_user_last_tweets(monkeypatch):
    calls = []

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                'data': {
                    'tweets': [
                        {'id': 'tweet-1', 'text': 'vibe check $ANSEM'},
                    ],
                    'has_next_page': False,
                }
            }

    def fake_get(url, *, params=None, headers=None, timeout=None):
        calls.append({'url': url, 'params': params, 'headers': headers, 'timeout': timeout})
        return FakeResponse()

    monkeypatch.setattr('rh_meme_sniper.sources.x_source.httpx.get', fake_get)
    provider = TwitterAPIIOProvider(api_key='test-key')

    tweets = provider.get_user_tweets(user_name='blknoiz06', max_items=5)

    assert tweets == [{'id': 'tweet-1', 'text': 'vibe check $ANSEM'}]
    assert calls[0]['url'] == 'https://api.twitterapi.io/twitter/user/last_tweets'
    assert calls[0]['params']['userName'] == 'blknoiz06'
    assert calls[0]['headers']['X-API-Key'] == 'test-key'


def test_twitterapiio_provider_maps_latest_plus_top_to_latest(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {'tweets': [], 'has_next_page': False, 'next_cursor': ''}

    captured = {}

    def fake_get(url, *, params=None, headers=None, timeout=None):
        captured['params'] = params
        return FakeResponse()

    monkeypatch.setattr('rh_meme_sniper.sources.x_source.httpx.get', fake_get)
    provider = TwitterAPIIOProvider(api_key='test-key')

    provider.search_tweets(query='office cat', max_items=5, sort='Latest + Top', tweet_language='en')

    assert captured['params']['queryType'] == 'Latest'


def test_twitterapiio_provider_surfaces_credit_error_message(monkeypatch):
    import httpx

    class FakeResponse:
        status_code = 402
        text = '{"error":"Unauthorized","message":"Credits is not enough.Please recharge"}'

        def raise_for_status(self):
            request = httpx.Request('GET', 'https://api.twitterapi.io/twitter/tweet/advanced_search')
            response = httpx.Response(402, request=request, text=self.text)
            raise httpx.HTTPStatusError('402 Payment Required', request=request, response=response)

        def json(self):
            return {'error': 'Unauthorized', 'message': 'Credits is not enough.Please recharge'}

    def fake_get(url, *, params=None, headers=None, timeout=None):
        return FakeResponse()

    monkeypatch.setattr('rh_meme_sniper.sources.x_source.httpx.get', fake_get)
    provider = TwitterAPIIOProvider(api_key='test-key')

    with __import__('pytest').raises(RuntimeError, match='Credits is not enough'):
        provider.search_tweets(query='cashcat robinhood', max_items=2, sort='Latest', tweet_language='en')


def test_twitterapiio_provider_reads_account_balance(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {'recharge_credits': 12345, 'total_bonus_credits': 67}

    captured = {}

    def fake_get(url, *, params=None, headers=None, timeout=None):
        captured['url'] = url
        captured['headers'] = headers
        return FakeResponse()

    monkeypatch.setattr('rh_meme_sniper.sources.x_source.httpx.get', fake_get)
    provider = TwitterAPIIOProvider(api_key='test-key')

    balance = provider.get_account_balance()

    assert captured['url'] == 'https://api.twitterapi.io/oapi/my/info'
    assert captured['headers']['x-api-key'] == 'test-key'
    assert balance == {'recharge_credits': 12345, 'total_bonus_credits': 67}


def test_filter_pair_items_respects_allowlist_and_denylist():
    pair_items = [
        {
            'pairAddress': 'pair-cashcat',
            'chainId': 'robinhood',
            'baseToken': {'address': '0x1111111111111111111111111111111111111111', 'name': 'Cash Cat', 'symbol': 'CASHCAT'},
            'url': 'https://dexscreener.com/robinhood/pair-cashcat',
        },
        {
            'pairAddress': 'pair-baby-cashcat',
            'chainId': 'robinhood',
            'baseToken': {'address': '0x2222222222222222222222222222222222222222', 'name': 'Baby Cash Cat', 'symbol': 'BCAT'},
            'url': 'https://dexscreener.com/robinhood/pair-baby-cashcat',
        },
        {
            'pairAddress': 'pair-pepe',
            'chainId': 'robinhood',
            'baseToken': {'address': '0x3333333333333333333333333333333333333333', 'name': 'Robinhood Pepe', 'symbol': 'RHPEPE'},
            'url': 'https://dexscreener.com/robinhood/pair-pepe',
        },
    ]

    filtered = filter_pair_items(
        pair_items,
        allow_terms=['cash cat'],
        deny_terms=['baby'],
    )

    assert [item['pairAddress'] for item in filtered] == ['pair-cashcat']


def test_run_alert_loop_from_payload_applies_pair_filters():
    real_ca = '0x1111111111111111111111111111111111111111'
    unrelated_ca = '0x2222222222222222222222222222222222222222'
    payload = {
        'tweets': [
            {
                'id': 't1',
                'text': f'Cash Cat on Robinhood CA {real_ca}',
                'createdAt': '2026-07-09T01:18:00Z',
                'author': {'userName': 'kol_alpha'},
                'url': 'https://x.com/a/status/1',
                'likeCount': 200,
                'retweetCount': 25,
            }
        ],
        'pairs': [
            {
                'pairAddress': 'pair-real',
                'chainId': 'robinhood',
                'baseToken': {'address': real_ca, 'name': 'Cash Cat', 'symbol': 'CASHCAT'},
                'url': 'https://dexscreener.com/robinhood/pair-real',
                'pairCreatedAt': 1783560000000,
                'liquidity': {'usd': 25000},
                'volume': {'h24': 200000, 'h1': 25000},
                'txns': {'h1': {'buys': 200, 'sells': 160}},
            },
            {
                'pairAddress': 'pair-unrelated',
                'chainId': 'robinhood',
                'baseToken': {'address': unrelated_ca, 'name': 'Robinhood Pepe', 'symbol': 'RHPEPE'},
                'url': 'https://dexscreener.com/robinhood/pair-unrelated',
                'pairCreatedAt': 1783560100000,
                'liquidity': {'usd': 50000},
                'volume': {'h24': 300000, 'h1': 40000},
                'txns': {'h1': {'buys': 250, 'sells': 170}},
            },
        ],
    }

    artifacts = run_alert_loop_from_payload(
        query='cashcat robinhood',
        payload=payload,
        output_dir=Path('./outputs'),
        send_telegram=False,
        pair_allow_terms=['cash cat'],
        pair_deny_terms=['pepe'],
    )

    assert artifacts.pair_count == 1
    assert artifacts.alert_count == 1
    assert all(cluster.cluster_id != 'pepe' for cluster in artifacts.clusters)


def test_cli_run_query_pack_executes_each_query(monkeypatch, tmp_path):
    pack_path = tmp_path / 'query-pack.json'
    pack_path.write_text(json.dumps({
        'actor_id': 'xtdata~twitter-x-scraper',
        'queries': [
            {'id': 'q1', 'query': 'cashcat robinhood', 'sort': 'Latest', 'max_items': 5},
            {'id': 'q2', 'query': 'office cat robinhood', 'sort': 'Top', 'max_items': 3},
        ],
    }))

    calls = []

    def fake_run_live_alert_loop(**kwargs):
        calls.append(kwargs)

        class Artifacts:
            tweet_count = 1
            pair_count = 1
            cluster_count = 1
            alert_count = 0
            output_paths = {'alerts': f"outputs/{kwargs['query'].replace(' ', '-')}.log"}
            alerts = []
            provider_name = 'apify'
            usage_summary = None

        return Artifacts()

    monkeypatch.setattr('rh_meme_sniper.pipeline.run_live_alert_loop', fake_run_live_alert_loop)

    result = runner.invoke(app, ['run-query-pack', str(pack_path)])

    assert result.exit_code == 0
    assert len(calls) == 2
    assert calls[0]['query'] == 'cashcat robinhood'
    assert calls[1]['query'] == 'office cat robinhood'
    assert 'cashcat robinhood' in result.stdout
    assert 'office cat robinhood' in result.stdout


def test_cli_run_alert_loop_includes_provider_usage_summary(monkeypatch):
    def fake_run_live_alert_loop(**kwargs):
        class Artifacts:
            tweet_count = 2
            pair_count = 3
            cluster_count = 2
            alert_count = 1
            output_paths = {'alerts': 'outputs/cashcat.log'}
            alerts = ['alert text']
            provider_name = 'twitterapiio'
            usage_summary = {
                'balance_before': {'recharge_credits': 1000, 'total_bonus_credits': 20},
                'balance_after': {'recharge_credits': 700, 'total_bonus_credits': 20},
                'credits_used_estimate': 300,
            }

        return Artifacts()

    monkeypatch.setattr('rh_meme_sniper.cli.run_live_alert_loop', fake_run_live_alert_loop)

    result = runner.invoke(app, ['run-alert-loop', 'cashcat robinhood', '--max-items', '2', '--sort', 'Latest'])

    assert result.exit_code == 0
    assert '"provider": "twitterapiio"' in result.stdout
    assert '"credits_used_estimate": 300' in result.stdout
    assert '"recharge_credits": 700' in result.stdout


def test_run_alert_loop_tolerates_balance_lookup_failure(monkeypatch):
    from rh_meme_sniper.pipeline import run_live_alert_loop

    class FakeProvider:
        provider_name = 'twitterapiio'

        def search_tweets(self, **kwargs):
            return []

        def get_account_balance(self):
            raise RuntimeError('balance endpoint timeout')

    monkeypatch.setattr('rh_meme_sniper.pipeline.get_x_provider', lambda actor_id=None: FakeProvider())
    monkeypatch.setattr('rh_meme_sniper.pipeline._fetch_pairs', lambda *args, **kwargs: [])

    artifacts = run_live_alert_loop(query='cashcat robinhood', max_items=2, send_telegram=False)

    assert artifacts.tweet_count == 0
    assert artifacts.usage_summary is None


def test_run_alert_loop_from_payload_suppresses_duplicate_alerts_with_seen_state(tmp_path):
    real_ca = '0x1111111111111111111111111111111111111111'
    payload = {
        'tweets': [
            {
                'id': 't1',
                'text': f'Robinhood Office Cat CA {real_ca}',
                'createdAt': '2026-07-09T01:18:00Z',
                'author': {'userName': 'kol_alpha'},
                'url': 'https://x.com/a/status/1',
                'likeCount': 200,
                'retweetCount': 25,
            }
        ],
        'pairs': [
            {
                'pairAddress': 'pair-real',
                'chainId': 'robinhood',
                'baseToken': {'address': real_ca, 'name': 'Robinhood Office Cat', 'symbol': 'ROC'},
                'url': 'https://dexscreener.com/robinhood/pair-real',
                'pairCreatedAt': 1783560000000,
                'liquidity': {'usd': 25000},
                'volume': {'h24': 200000, 'h1': 25000},
                'txns': {'h1': {'buys': 200, 'sells': 160}},
            }
        ],
    }
    state_db_path = tmp_path / 'state.db'

    first = run_alert_loop_from_payload(
        query='robinhood office cat',
        payload=payload,
        output_dir=tmp_path / 'out1',
        send_telegram=False,
        state_db_path=state_db_path,
        alert_cooldown_seconds=3600,
    )
    second = run_alert_loop_from_payload(
        query='robinhood office cat',
        payload=payload,
        output_dir=tmp_path / 'out2',
        send_telegram=False,
        state_db_path=state_db_path,
        alert_cooldown_seconds=3600,
    )

    assert first.alert_count == 1
    assert second.alert_count == 0
    assert second.alerts == []


def test_run_discovery_executes_watchlist_and_bucket_queries(monkeypatch, tmp_path):
    from rh_meme_sniper.pipeline import run_discovery

    watchlist_path = tmp_path / 'watchlist.json'
    watchlist_path.write_text(json.dumps({
        'primary_accounts': ['jiggacapital'],
        'secondary_accounts': ['DoxxedChannel'],
    }))

    query_buckets_path = tmp_path / 'query-buckets.json'
    query_buckets_path.write_text(json.dumps({
        'account_query_templates': [
            {'id_prefix': 'acct-rh', 'query_template': 'from:{handle} robinhood', 'sort': 'Latest', 'max_items': 5, 'require_tweet_match_for_alerts': True, 'max_tweet_age_days': 14},
        ],
        'keyword_queries': [
            {'id': 'cashcat', 'query': 'cashcat robinhood', 'sort': 'Top', 'max_items': 8},
        ],
        'pair_deny_terms': ['pepe'],
    }))

    calls = []

    def fake_run_live_alert_loop(**kwargs):
        calls.append(kwargs)

        class Artifacts:
            tweet_count = 1
            pair_count = 1
            cluster_count = 1
            alert_count = 0
            output_paths = {'alerts': f"outputs/{kwargs['query'].replace(' ', '-')}.log"}
            alerts = []
            clusters = []

        return Artifacts()

    monkeypatch.setattr('rh_meme_sniper.pipeline.run_live_alert_loop', fake_run_live_alert_loop)

    results = run_discovery(
        watchlist_path=watchlist_path,
        query_buckets_path=query_buckets_path,
        send_telegram=False,
    )

    assert len(results.runs) == 3
    assert calls[0]['query'] == 'from:jiggacapital robinhood'
    assert calls[1]['query'] == 'from:DoxxedChannel robinhood'
    assert calls[2]['query'] == 'cashcat robinhood'
    assert all(call['pair_deny_terms'] == ['pepe'] for call in calls)
    assert calls[0]['require_tweet_match_for_alerts'] is True
    assert calls[0]['max_tweet_age_days'] == 14


def test_cli_run_discovery_executes_configs(monkeypatch, tmp_path):
    watchlist_path = tmp_path / 'watchlist.json'
    watchlist_path.write_text(json.dumps({'primary_accounts': ['jiggacapital']}))
    query_buckets_path = tmp_path / 'query-buckets.json'
    query_buckets_path.write_text(json.dumps({'keyword_queries': [{'id': 'cashcat', 'query': 'cashcat robinhood'}]}))

    def fake_run_discovery(**kwargs):
        class Results:
            runs = [
                {'id': 'cashcat', 'query': 'cashcat robinhood', 'alert_count': 0, 'tweet_count': 1, 'pair_count': 1, 'cluster_count': 1, 'output_paths': {'alerts': 'outputs/cashcat.log'}},
            ]

        return Results()

    monkeypatch.setattr('rh_meme_sniper.cli.run_discovery', fake_run_discovery)

    result = runner.invoke(app, ['run-discovery', str(watchlist_path), str(query_buckets_path)])

    assert result.exit_code == 0
    assert 'cashcat robinhood' in result.stdout


def test_run_query_pack_records_tracking_state(monkeypatch, tmp_path):
    from rh_meme_sniper.pipeline import run_query_pack

    pack_path = tmp_path / 'query-pack.json'
    pack_path.write_text(json.dumps({
        'queries': [
            {'id': 'cashcat', 'query': 'cashcat robinhood', 'sort': 'Latest', 'max_items': 5},
        ],
    }))
    tracking_db_path = tmp_path / 'tracking.db'

    candidate = CandidateToken(
        cluster_id='cash-cat',
        contract_address='0x020bfC650A365f8BB26819deAAbF3E21291018b4',
        symbol='CASHCAT',
        name='Cash Cat',
        pair_address='pair-real',
        pair_created_at='2026-06-18T20:01:25Z',
        first_seen_x_at='2026-07-12T08:17:17Z',
        first_seen_ca_at='2026-07-12T08:17:17Z',
        first_seen_market_at='2026-07-12T08:17:17Z',
        first_kol_mentions=['robinhooddailys'],
        liquidity_usd=1000,
        volume_1h=200,
        volume_24h=5000,
        buy_count_1h=10,
        sell_count_1h=3,
        authenticity_score=95,
        timing_score=80,
        market_score=100,
        hype_score=30,
        alert_score=88,
        verdict='alert',
    )

    def fake_run_live_alert_loop(**kwargs):
        class Artifacts:
            tweet_count = 1
            pair_count = 1
            cluster_count = 1
            alert_count = 1
            output_paths = {'alerts': 'outputs/cashcat.log'}
            alerts = ['alert text']
            provider_name = 'twitterapiio'
            usage_summary = None
            clusters = [
                NarrativeCluster(
                    cluster_id='cash-cat',
                    canonical_name='Cash Cat',
                    related_contracts=[candidate.contract_address],
                    related_pairs=['pair-real'],
                    related_handles=['robinhooddailys'],
                    events=[
                        RawEvent(
                            source='x',
                            source_id='tweet-1',
                            observed_at='2026-07-12T08:17:17Z',
                            author_handle='robinhooddailys',
                            text='Cash Cat $CASHCAT 0x020bfC650A365f8BB26819deAAbF3E21291018b4',
                            contract_addresses=[candidate.contract_address],
                        )
                    ],
                    candidates=[candidate],
                    canonical_candidate=candidate,
                    status='likely_canonical',
                )
            ]

        return Artifacts()

    monkeypatch.setattr('rh_meme_sniper.pipeline.run_live_alert_loop', fake_run_live_alert_loop)

    results = run_query_pack(
        query_pack_path=pack_path,
        send_telegram=False,
        tracking_db_path=tracking_db_path,
    )

    assert len(results.runs) == 1

    import sqlite3
    with sqlite3.connect(tracking_db_path) as conn:
        tracked = conn.execute('SELECT contract_address, symbol, query FROM tracked_tokens').fetchall()
        mentions = conn.execute('SELECT source_id, author_handle, query FROM mentions').fetchall()
        snapshots = conn.execute('SELECT contract_address, liquidity_usd, volume_1h FROM market_snapshots').fetchall()

    assert tracked == [('0x020bfC650A365f8BB26819deAAbF3E21291018b4', 'CASHCAT', 'cashcat robinhood')]
    assert mentions == [('tweet-1', 'robinhooddailys', 'cashcat robinhood')]
    assert snapshots == [('0x020bfC650A365f8BB26819deAAbF3E21291018b4', 1000.0, 200.0)]


def test_run_kol_scan_records_entity_events_from_watchlist_activity(tmp_path, monkeypatch):
    from rh_meme_sniper.pipeline import run_kol_scan

    watchlist_path = tmp_path / 'watchlist.json'
    watchlist_path.write_text(json.dumps({
        'primary_accounts': ['blknoiz06'],
    }))
    tracking_db_path = tmp_path / 'tracking.db'

    class FakeProvider:
        provider_name = 'fake-x'

        def get_account_balance(self):
            return None

        def get_user_tweets(self, *, user_name, max_items):
            assert user_name == 'blknoiz06'
            assert max_items == 3
            return [
                {
                    'id': 'tweet-ansem-1',
                    'text': 'vibe check $ANSEM stimmy for the trenches 9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump',
                    'url': 'https://x.com/blknoiz06/status/1',
                    'createdAt': '2026-07-13T01:00:00Z',
                    'author': {'userName': 'blknoiz06'},
                }
            ]

    monkeypatch.setattr('rh_meme_sniper.pipeline.get_x_provider', lambda actor_id=None: FakeProvider())

    results = run_kol_scan(
        watchlist_path=watchlist_path,
        tracking_db_path=tracking_db_path,
        max_items=3,
        max_tweet_age_days=14,
    )

    assert results.runs == [
        {
            'handle': 'blknoiz06',
            'query': 'from:blknoiz06',
            'tweet_count': 1,
            'event_count': 1,
        }
    ]
    with sqlite3.connect(tracking_db_path) as conn:
        entity = conn.execute('SELECT entity_key, entity_type, display_name FROM tracked_entities').fetchone()
        event = conn.execute(
            'SELECT entity_key, source_id, event_type, symbols_json, contracts_json, text FROM entity_events'
        ).fetchone()

    assert entity == ('x:blknoiz06', 'x_handle', 'blknoiz06')
    assert event[0] == 'x:blknoiz06'
    assert event[1] == 'tweet-ansem-1'
    assert event[2] == 'creator_signal'
    assert json.loads(event[3]) == ['ANSEM']
    assert json.loads(event[4]) == ['9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump']
    assert 'vibe check' in event[5]


def test_rescan_tracked_tokens_reads_tracking_db_and_refreshes_snapshots(tmp_path, monkeypatch):
    from rh_meme_sniper.pipeline import rescan_tracked_tokens
    from rh_meme_sniper.state import TrackingState

    tracking_db_path = tmp_path / 'tracking.db'
    state = TrackingState(tracking_db_path)
    state.record_candidate(
        query='cashcat robinhood',
        candidate=CandidateToken(
            cluster_id='cash-cat',
            contract_address='0x020bfC650A365f8BB26819deAAbF3E21291018b4',
            symbol='CASHCAT',
            name='Cash Cat',
            pair_address='pair-old',
            liquidity_usd=1000,
            volume_1h=200,
            volume_24h=5000,
            buy_count_1h=10,
            sell_count_1h=3,
            authenticity_score=95,
            timing_score=80,
            market_score=100,
            hype_score=30,
            alert_score=88,
            verdict='alert',
        ),
        cluster=NarrativeCluster(cluster_id='cash-cat', canonical_name='Cash Cat'),
    )

    monkeypatch.setattr('rh_meme_sniper.pipeline._fetch_pairs', lambda query_terms, chain_id=None, limit_per_query=5: [
        {
            'pairAddress': 'pair-new',
            'chainId': 'robinhood',
            'baseToken': {'address': '0x020bfC650A365f8BB26819deAAbF3E21291018b4', 'name': 'Cash Cat', 'symbol': 'CASHCAT'},
            'url': 'https://dexscreener.com/robinhood/pair-new',
            'pairCreatedAt': 1783560000000,
            'liquidity': {'usd': 2222},
            'volume': {'h24': 8888, 'h1': 333},
            'txns': {'h1': {'buys': 20, 'sells': 5}},
        }
    ])

    results = rescan_tracked_tokens(tracking_db_path=tracking_db_path)

    assert results['tracked_token_count'] == 1
    assert results['rescanned_count'] == 1
    assert results['runs'][0]['contract_address'] == '0x020bfC650A365f8BB26819deAAbF3E21291018b4'
    assert results['runs'][0]['liquidity_usd'] == 2222.0

    import sqlite3
    with sqlite3.connect(tracking_db_path) as conn:
        latest = conn.execute(
            'SELECT contract_address, pair_address, liquidity_usd, volume_1h FROM market_snapshots ORDER BY id DESC LIMIT 1'
        ).fetchone()

    assert latest == ('0x020bfC650A365f8BB26819deAAbF3E21291018b4', 'pair-new', 2222.0, 333.0)


def test_tracking_state_prune_removes_old_mentions_and_snapshots_but_keeps_tracked_tokens(tmp_path):
    from rh_meme_sniper.state import TrackingState

    tracking_db_path = tmp_path / 'tracking.db'
    state = TrackingState(tracking_db_path)
    candidate = CandidateToken(
        cluster_id='cash-cat',
        contract_address='0x020bfC650A365f8BB26819deAAbF3E21291018b4',
        symbol='CASHCAT',
        name='Cash Cat',
        pair_address='pair-old',
        liquidity_usd=1000,
        volume_1h=200,
        volume_24h=5000,
        buy_count_1h=10,
        sell_count_1h=3,
        authenticity_score=95,
        timing_score=80,
        market_score=100,
        hype_score=30,
        alert_score=88,
        verdict='alert',
    )
    state.record_candidate(query='cashcat robinhood', candidate=candidate, cluster=NarrativeCluster(cluster_id='cash-cat', canonical_name='Cash Cat'))
    state.record_mention(
        query='cashcat robinhood',
        contract_address=candidate.contract_address,
        event=RawEvent(source='x', source_id='tweet-1', observed_at='2026-07-12T08:17:17Z', author_handle='robinhooddailys', text='Cash Cat'),
    )

    old_ts = (datetime.now(timezone.utc) - timedelta(days=200)).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
    with sqlite3.connect(tracking_db_path) as conn:
        conn.execute("UPDATE mentions SET created_at = ?", (old_ts,))
        conn.execute("UPDATE market_snapshots SET captured_at = ?", (old_ts,))
        conn.execute("UPDATE tracked_tokens SET last_updated_at = ?", (old_ts,))

    summary = state.prune(retention_days=180)

    assert summary['deleted_mentions'] == 1
    assert summary['deleted_market_snapshots'] == 1
    assert summary['deleted_tracked_tokens'] == 0
    with sqlite3.connect(tracking_db_path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM tracked_tokens').fetchone()[0] == 1
        assert conn.execute('SELECT COUNT(*) FROM mentions').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM market_snapshots').fetchone()[0] == 0


def test_cli_run_kol_scan_outputs_summary(tmp_path, monkeypatch):
    watchlist_path = tmp_path / 'watchlist.json'
    watchlist_path.write_text(json.dumps({'primary_accounts': ['blknoiz06']}))
    tracking_db_path = tmp_path / 'tracking.db'

    class Results:
        runs = [{'handle': 'blknoiz06', 'query': 'from:blknoiz06', 'tweet_count': 1, 'event_count': 1}]

    def fake_run_kol_scan(**kwargs):
        assert kwargs['watchlist_path'] == watchlist_path
        assert kwargs['tracking_db_path'] == tracking_db_path
        assert kwargs['max_items'] == 3
        return Results()

    monkeypatch.setattr('rh_meme_sniper.cli.run_kol_scan', fake_run_kol_scan)

    result = runner.invoke(app, ['run-kol-scan', str(watchlist_path), '--tracking-db-path', str(tracking_db_path), '--max-items', '3'])

    assert result.exit_code == 0
    assert '"run_count": 1' in result.stdout
    assert 'from:blknoiz06' in result.stdout


def test_cli_prune_tracking_db_outputs_summary(tmp_path):
    from rh_meme_sniper.state import TrackingState

    tracking_db_path = tmp_path / 'tracking.db'
    state = TrackingState(tracking_db_path)
    candidate = CandidateToken(
        cluster_id='cash-cat',
        contract_address='0x020bfC650A365f8BB26819deAAbF3E21291018b4',
        symbol='CASHCAT',
        name='Cash Cat',
        pair_address='pair-old',
        liquidity_usd=1000,
        volume_1h=200,
        volume_24h=5000,
        buy_count_1h=10,
        sell_count_1h=3,
        verdict='alert',
    )
    state.record_candidate(query='cashcat robinhood', candidate=candidate, cluster=NarrativeCluster(cluster_id='cash-cat', canonical_name='Cash Cat'))
    with sqlite3.connect(tracking_db_path) as conn:
        conn.execute(
            "UPDATE market_snapshots SET captured_at = ?",
            (((datetime.now(timezone.utc) - timedelta(days=190)).replace(microsecond=0).isoformat().replace('+00:00', 'Z')),),
        )

    result = runner.invoke(app, ['prune-tracking-db', str(tracking_db_path), '--retention-days', '180'])

    assert result.exit_code == 0
    assert '"deleted_market_snapshots": 1' in result.stdout


def test_render_candidate_alert_includes_dexscreener_link_and_copyable_ca():
    candidate = CandidateToken(
        cluster_id='cash-cat',
        contract_address='0x020bfC650A365f8BB26819deAAbF3E21291018b4',
        symbol='CASHCAT',
        name='Cash Cat',
        authenticity_score=95.0,
        market_score=100.0,
        alert_score=89.5,
        liquidity_usd=11070968.34,
        volume_1h=1911362.85,
        verdict='alert',
    )

    text = render_candidate_alert(candidate)

    assert '<a href="https://dexscreener.com/robinhood/0x020bfC650A365f8BB26819deAAbF3E21291018b4">DexScreener</a>' in text
    assert 'CA: <code>0x020bfC650A365f8BB26819deAAbF3E21291018b4</code>' in text


def test_rule_judge_marks_generic_broad_candidate_as_ignore():
    from rh_meme_sniper.pipeline import judge_candidate_tracking_status

    candidate = CandidateToken(
        cluster_id='favorite-meme',
        contract_address='0x79Fe86b963255Ce884bdcaC6388C50a599Ba277f',
        symbol='ROBINHOOD',
        name='Robinhood',
        liquidity_usd=2000,
        volume_1h=100,
        volume_24h=500,
        verdict='watch',
    )

    status, reason = judge_candidate_tracking_status(
        query='"favorite meme" robinhood',
        candidate=candidate,
        cluster=NarrativeCluster(cluster_id='favorite-meme', canonical_name='Robinhood'),
    )

    assert status == 'ignore'
    assert reason == 'generic_or_brand_term'


def test_rule_judge_marks_specific_ca_candidate_as_strong_candidate():
    from rh_meme_sniper.pipeline import judge_candidate_tracking_status

    candidate = CandidateToken(
        cluster_id='cash-cat',
        contract_address='0x020bfC650A365f8BB26819deAAbF3E21291018b4',
        symbol='CASHCAT',
        name='Cash Cat',
        first_seen_ca_at='2026-07-12T08:17:17Z',
        liquidity_usd=12000,
        volume_1h=2500,
        volume_24h=30000,
        buy_count_1h=30,
        sell_count_1h=10,
        alert_score=88,
        verdict='alert',
    )

    status, reason = judge_candidate_tracking_status(
        query='cashcat robinhood',
        candidate=candidate,
        cluster=NarrativeCluster(cluster_id='cash-cat', canonical_name='Cash Cat'),
    )

    assert status == 'strong_candidate'
    assert reason in {'high_signal_candidate', 'canonical_exact_match_boost'}


def test_rule_judge_applies_canonical_exact_match_boost_for_exact_query_match():
    from rh_meme_sniper.pipeline import judge_candidate_tracking_status

    candidate = CandidateToken(
        cluster_id='cash-cat',
        contract_address='0x020bfC650A365f8BB26819deAAbF3E21291018b4',
        symbol='CASHCAT',
        name='Cash Cat',
        first_seen_ca_at='2026-07-12T08:17:17Z',
        liquidity_usd=9000,
        volume_24h=22000,
        authenticity_score=95,
        market_score=97,
        alert_score=72,
        verdict='watch',
    )

    status, reason = judge_candidate_tracking_status(
        query='cashcat robinhood',
        candidate=candidate,
        cluster=NarrativeCluster(cluster_id='cash-cat', canonical_name='Cash Cat'),
    )

    assert status == 'strong_candidate'
    assert reason == 'canonical_exact_match_boost'


def test_rule_judge_keeps_babycashcat_as_watch_not_ignore():
    from rh_meme_sniper.pipeline import judge_candidate_tracking_status

    candidate = CandidateToken(
        cluster_id='cash-cat',
        contract_address='0x57a1AB439e8C24B90Ecc6534C05621d6E68ED35A',
        symbol='BABYCASHCAT',
        name='Baby Cash Cat',
        first_seen_market_at='2026-07-12T08:17:17Z',
        liquidity_usd=39000,
        volume_24h=296000,
        verdict='watch',
    )

    status, reason = judge_candidate_tracking_status(
        query='cashcat robinhood',
        candidate=candidate,
        cluster=NarrativeCluster(cluster_id='cash-cat', canonical_name='Cash Cat'),
    )

    assert status == 'watch'
    assert reason in {'has_contract_signal', 'derivative_variant'}
    assert reason != 'canonical_exact_match_boost'


def test_rule_judge_ignores_extracted_tweet_blob_as_name():
    from rh_meme_sniper.pipeline import judge_candidate_tracking_status

    candidate = CandidateToken(
        cluster_id='cash-cat',
        contract_address='0xC070B017da80D71b102971232BD885252c64394f',
        name='$CASHCAT $ARROW delivered an 88x return 🚀 now ChillHood is next. On Robinhood CA 0xC070B017da80D71b102971232BD885252c64394f',
        first_seen_ca_at='2026-07-12T08:17:17Z',
        verdict='watch',
    )

    status, reason = judge_candidate_tracking_status(
        query='cashcat robinhood',
        candidate=candidate,
        cluster=NarrativeCluster(cluster_id='cash-cat', canonical_name='Cash Cat'),
    )

    assert status == 'ignore'
    assert reason == 'garbage_extracted_name'


def test_rule_judge_ignores_generic_robinhood_brand_derivative():
    from rh_meme_sniper.pipeline import judge_candidate_tracking_status

    candidate = CandidateToken(
        cluster_id='favorite-meme',
        contract_address='0x94FEf3763ED87051267dCd7FfC5DF416B6C03a7E',
        symbol='RPP402',
        name='Robinhood Payments Protocol',
        first_seen_market_at='2026-07-12T08:17:17Z',
        liquidity_usd=29982,
        volume_24h=36388,
        verdict='alert',
        alert_score=87,
    )

    status, reason = judge_candidate_tracking_status(
        query='"favorite meme" robinhood',
        candidate=candidate,
        cluster=NarrativeCluster(cluster_id='favorite-meme', canonical_name='Robinhood Payments Protocol'),
    )

    assert status == 'ignore'
    assert reason == 'generic_robinhood_brand_derivative'


def test_apply_tracking_judge_keeps_hard_ignore_guardrail_over_llm_override():
    from rh_meme_sniper.pipeline import apply_tracking_judge

    class TooPermissiveJudge:
        def judge(self, *, query, candidate, cluster):
            return {'status': 'watch', 'reason': 'llm_override'}

    candidate = CandidateToken(
        cluster_id='favorite-meme',
        contract_address='0x94FEf3763ED87051267dCd7FfC5DF416B6C03a7E',
        symbol='WALLET',
        name='Robinhood Wallet',
        first_seen_market_at='2026-07-12T08:17:17Z',
        liquidity_usd=29982,
        volume_24h=36388,
        verdict='alert',
        alert_score=87,
    )

    status, reason = apply_tracking_judge(
        query='"favorite meme" robinhood',
        candidate=candidate,
        cluster=NarrativeCluster(cluster_id='favorite-meme', canonical_name='Robinhood Wallet'),
        llm_judge=TooPermissiveJudge(),
    )

    assert status == 'ignore'
    assert reason == 'generic_robinhood_brand_derivative'


def test_apply_tracking_judge_keeps_canonical_boost_over_llm_downgrade():
    from rh_meme_sniper.pipeline import apply_tracking_judge

    class TooConservativeJudge:
        def judge(self, *, query, candidate, cluster):
            return {'status': 'watch', 'reason': 'llm_too_conservative'}

    candidate = CandidateToken(
        cluster_id='cash-cat',
        contract_address='0x020bfC650A365f8BB26819deAAbF3E21291018b4',
        symbol='CASHCAT',
        name='Cash Cat',
        first_seen_ca_at='2026-07-12T08:17:17Z',
        liquidity_usd=9000,
        volume_24h=22000,
        authenticity_score=95,
        market_score=97,
        alert_score=72,
        verdict='watch',
    )

    status, reason = apply_tracking_judge(
        query='cashcat robinhood',
        candidate=candidate,
        cluster=NarrativeCluster(cluster_id='cash-cat', canonical_name='Cash Cat'),
        llm_judge=TooConservativeJudge(),
    )

    assert status == 'strong_candidate'
    assert reason == 'canonical_exact_match_boost'


def test_apply_tracking_judge_uses_llm_override_when_available():
    from rh_meme_sniper.pipeline import apply_tracking_judge

    class FakeJudge:
        def judge(self, *, query, candidate, cluster):
            return {'status': 'ignore', 'reason': 'llm_context_reject'}

    candidate = CandidateToken(
        cluster_id='baby-cash-cat',
        contract_address='0x57a1AB439e8C24B90Ecc6534C05621d6E68ED35A',
        symbol='BABYCASHCAT',
        name='Baby Cash Cat',
        first_seen_market_at='2026-07-12T08:17:17Z',
        liquidity_usd=39000,
        volume_24h=296000,
        alert_score=70,
        verdict='watch',
    )

    status, reason = apply_tracking_judge(
        query='cashcat robinhood',
        candidate=candidate,
        cluster=NarrativeCluster(cluster_id='cash-cat', canonical_name='Cash Cat'),
        llm_judge=FakeJudge(),
    )

    assert status == 'ignore'
    assert reason == 'llm_context_reject'


def test_apply_tracking_judge_falls_back_to_rule_based_on_llm_error():
    from rh_meme_sniper.pipeline import apply_tracking_judge

    class BrokenJudge:
        def judge(self, *, query, candidate, cluster):
            raise RuntimeError('timeout')

    candidate = CandidateToken(
        cluster_id='cash-cat',
        contract_address='0x020bfC650A365f8BB26819deAAbF3E21291018b4',
        symbol='CASHCAT',
        name='Cash Cat',
        first_seen_ca_at='2026-07-12T08:17:17Z',
        liquidity_usd=12000,
        volume_1h=2500,
        volume_24h=30000,
        buy_count_1h=30,
        sell_count_1h=10,
        alert_score=88,
        verdict='alert',
    )

    status, reason = apply_tracking_judge(
        query='cashcat robinhood',
        candidate=candidate,
        cluster=NarrativeCluster(cluster_id='cash-cat', canonical_name='Cash Cat'),
        llm_judge=BrokenJudge(),
    )

    assert status == 'strong_candidate'
    assert reason in {'high_signal_candidate', 'canonical_exact_match_boost'}
