import json
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

        return Artifacts()

    monkeypatch.setattr('rh_meme_sniper.pipeline.run_live_alert_loop', fake_run_live_alert_loop)

    result = runner.invoke(app, ['run-query-pack', str(pack_path)])

    assert result.exit_code == 0
    assert len(calls) == 2
    assert calls[0]['query'] == 'cashcat robinhood'
    assert calls[1]['query'] == 'office cat robinhood'
    assert 'cashcat robinhood' in result.stdout
    assert 'office cat robinhood' in result.stdout


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
            {'id_prefix': 'acct-rh', 'query_template': 'from:{handle} robinhood', 'sort': 'Latest', 'max_items': 5},
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
