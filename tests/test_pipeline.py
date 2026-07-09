from rh_meme_sniper.extract.ca_parser import extract_contract_addresses, extract_urls
from rh_meme_sniper.sources.apify_x import normalize_tweet_item, build_search_input
from rh_meme_sniper.sources.dexscreener import normalize_pair_item
from rh_meme_sniper.cluster.narrative_cluster import cluster_events
from rh_meme_sniper.score.authenticity import score_clusters
from rh_meme_sniper.alerts.telegram import render_candidate_alert
from rh_meme_sniper.cli import app

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
