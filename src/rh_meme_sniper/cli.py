from __future__ import annotations

import json
from pathlib import Path

from typing_extensions import Annotated

import typer

from rh_meme_sniper.alerts.telegram import render_candidate_alert
from rh_meme_sniper.cluster.narrative_cluster import cluster_events
from rh_meme_sniper.score.authenticity import score_clusters
from rh_meme_sniper.sources.apify_x import normalize_tweet_item
from rh_meme_sniper.sources.dexscreener import normalize_pair_item

app = typer.Typer(add_completion=False)


@app.callback()
def main() -> None:
    """Robinhood meme sniper CLI."""


@app.command("analyze-sample")
def analyze_sample(sample_path: Annotated[Path, typer.Argument(exists=True, readable=True)]) -> None:
    payload = json.loads(sample_path.read_text())
    tweet_events = [normalize_tweet_item(item) for item in payload.get("tweets", [])]
    pair_events = [normalize_pair_item(item) for item in payload.get("pairs", [])]
    clusters = score_clusters(cluster_events(tweet_events + pair_events))
    for cluster in clusters:
        if cluster.canonical_candidate:
            typer.echo(render_candidate_alert(cluster.canonical_candidate))
            typer.echo("---")


if __name__ == "__main__":
    app()
