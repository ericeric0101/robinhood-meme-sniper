from __future__ import annotations

import json
from pathlib import Path

from typing_extensions import Annotated

import typer

from rh_meme_sniper.pipeline import run_alert_loop_from_payload, run_live_alert_loop, run_query_pack, run_discovery, rescan_tracked_tokens, run_kol_scan
from rh_meme_sniper.state import TrackingState
from rh_meme_sniper.config import settings
from rh_meme_sniper.sources.apify_client import ApifyAccessError
from rh_meme_sniper.sources.x_source import TwitterAPIIOAccessError

app = typer.Typer(add_completion=False)


@app.callback()
def main() -> None:
    """Robinhood meme sniper CLI."""


@app.command("analyze-sample")
def analyze_sample(sample_path: Annotated[Path, typer.Argument(exists=True, readable=True)]) -> None:
    artifacts = run_alert_loop_from_payload(
        query=sample_path.stem,
        payload=json.loads(sample_path.read_text()),
        output_dir=Path("./outputs"),
        send_telegram=False,
    )
    for alert_text in artifacts.alerts:
        typer.echo(alert_text)
        typer.echo("---")


@app.command("apify-search")
def apify_search(
    query: Annotated[str, typer.Argument()],
    max_items: Annotated[int, typer.Option("--max-items", min=1)] = 100,
    sort: Annotated[str, typer.Option("--sort")] = "Top",
    actor_id: Annotated[str | None, typer.Option("--actor-id")] = None,
    pair_allow_terms: Annotated[list[str], typer.Option("--pair-allow-term")] = [],
    pair_deny_terms: Annotated[list[str], typer.Option("--pair-deny-term")] = [],
) -> None:
    try:
        artifacts = run_live_alert_loop(
            query=query,
            max_items=max_items,
            sort=sort,
            send_telegram=False,
            actor_id=actor_id,
            pair_allow_terms=pair_allow_terms,
            pair_deny_terms=pair_deny_terms,
        )
    except (ApifyAccessError, TwitterAPIIOAccessError) as exc:
        typer.echo(str(exc))
        raise typer.Exit(2) from exc

    typer.echo(json.dumps({
        "tweet_count": artifacts.tweet_count,
        "pair_count": artifacts.pair_count,
        "cluster_count": artifacts.cluster_count,
        "alert_count": artifacts.alert_count,
        "provider": artifacts.provider_name,
        "usage_summary": artifacts.usage_summary,
        "output_paths": artifacts.output_paths,
    }, ensure_ascii=False, indent=2))


@app.command("run-alert-loop")
def run_alert_loop(
    query: Annotated[str, typer.Argument()],
    max_items: Annotated[int, typer.Option("--max-items", min=1)] = 100,
    sort: Annotated[str, typer.Option("--sort")] = "Top",
    send_telegram: Annotated[bool, typer.Option("--send-telegram/--no-send-telegram")] = False,
    sample_path: Annotated[Path | None, typer.Option("--sample-path", exists=True, readable=True)] = None,
    actor_id: Annotated[str | None, typer.Option("--actor-id")] = None,
    pair_allow_terms: Annotated[list[str], typer.Option("--pair-allow-term")] = [],
    pair_deny_terms: Annotated[list[str], typer.Option("--pair-deny-term")] = [],
    state_db_path: Annotated[Path | None, typer.Option("--state-db-path")] = None,
    alert_cooldown_seconds: Annotated[int, typer.Option("--alert-cooldown-seconds", min=0)] = 3600,
) -> None:
    if sample_path is not None:
        artifacts = run_alert_loop_from_payload(
            query=query,
            payload=json.loads(sample_path.read_text()),
            output_dir=Path("./outputs"),
            send_telegram=send_telegram,
            pair_allow_terms=pair_allow_terms,
            pair_deny_terms=pair_deny_terms,
            state_db_path=state_db_path,
            alert_cooldown_seconds=alert_cooldown_seconds,
        )
    else:
        try:
            artifacts = run_live_alert_loop(
                query=query,
                max_items=max_items,
                sort=sort,
                send_telegram=send_telegram,
                actor_id=actor_id,
                pair_allow_terms=pair_allow_terms,
                pair_deny_terms=pair_deny_terms,
                state_db_path=state_db_path,
                alert_cooldown_seconds=alert_cooldown_seconds,
            )
        except (ApifyAccessError, TwitterAPIIOAccessError) as exc:
            typer.echo(str(exc))
            raise typer.Exit(2) from exc

    typer.echo(json.dumps({
        "tweet_count": artifacts.tweet_count,
        "pair_count": artifacts.pair_count,
        "cluster_count": artifacts.cluster_count,
        "alert_count": artifacts.alert_count,
        "provider": artifacts.provider_name,
        "usage_summary": artifacts.usage_summary,
        "output_paths": artifacts.output_paths,
    }, ensure_ascii=False, indent=2))

    for alert_text in artifacts.alerts:
        typer.echo("---")
        typer.echo(alert_text)


@app.command("run-query-pack")
def run_query_pack_command(
    query_pack_path: Annotated[Path, typer.Argument(exists=True, readable=True)],
    send_telegram: Annotated[bool, typer.Option("--send-telegram/--no-send-telegram")] = False,
    state_db_path: Annotated[Path | None, typer.Option("--state-db-path")] = None,
    alert_cooldown_seconds: Annotated[int, typer.Option("--alert-cooldown-seconds", min=0)] = 3600,
    tracking_db_path: Annotated[Path | None, typer.Option("--tracking-db-path")] = settings.tracking_db_path,
) -> None:
    try:
        results = run_query_pack(
            query_pack_path=query_pack_path,
            send_telegram=send_telegram,
            state_db_path=state_db_path,
            alert_cooldown_seconds=alert_cooldown_seconds,
            tracking_db_path=tracking_db_path,
        )
    except (ApifyAccessError, TwitterAPIIOAccessError) as exc:
        typer.echo(str(exc))
        raise typer.Exit(2) from exc

    typer.echo(json.dumps({
        "actor_id": results.actor_id,
        "run_count": len(results.runs),
        "runs": results.runs,
    }, ensure_ascii=False, indent=2))


@app.command("run-discovery")
def run_discovery_command(
    watchlist_path: Annotated[Path, typer.Argument(exists=True, readable=True)],
    query_buckets_path: Annotated[Path, typer.Argument(exists=True, readable=True)],
    send_telegram: Annotated[bool, typer.Option("--send-telegram/--no-send-telegram")] = False,
    actor_id: Annotated[str | None, typer.Option("--actor-id")] = None,
    state_db_path: Annotated[Path | None, typer.Option("--state-db-path")] = None,
    alert_cooldown_seconds: Annotated[int, typer.Option("--alert-cooldown-seconds", min=0)] = 3600,
    tracking_db_path: Annotated[Path | None, typer.Option("--tracking-db-path")] = settings.tracking_db_path,
) -> None:
    try:
        results = run_discovery(
            watchlist_path=watchlist_path,
            query_buckets_path=query_buckets_path,
            send_telegram=send_telegram,
            actor_id=actor_id,
            state_db_path=state_db_path,
            alert_cooldown_seconds=alert_cooldown_seconds,
            tracking_db_path=tracking_db_path,
        )
    except (ApifyAccessError, TwitterAPIIOAccessError) as exc:
        typer.echo(str(exc))
        raise typer.Exit(2) from exc

    typer.echo(json.dumps({
        "run_count": len(results.runs),
        "runs": results.runs,
    }, ensure_ascii=False, indent=2))


@app.command("run-kol-scan")
def run_kol_scan_command(
    watchlist_path: Annotated[Path, typer.Argument(exists=True, readable=True)],
    tracking_db_path: Annotated[Path, typer.Option("--tracking-db-path")] = settings.tracking_db_path,
    max_items: Annotated[int, typer.Option("--max-items", min=1)] = 5,
    sort: Annotated[str, typer.Option("--sort")] = "Latest",
    max_tweet_age_days: Annotated[int | None, typer.Option("--max-tweet-age-days", min=1)] = 14,
    actor_id: Annotated[str | None, typer.Option("--actor-id")] = None,
) -> None:
    try:
        results = run_kol_scan(
            watchlist_path=watchlist_path,
            tracking_db_path=tracking_db_path,
            max_items=max_items,
            sort=sort,
            max_tweet_age_days=max_tweet_age_days,
            actor_id=actor_id,
        )
    except (ApifyAccessError, TwitterAPIIOAccessError) as exc:
        typer.echo(str(exc))
        raise typer.Exit(2) from exc

    typer.echo(json.dumps({
        "run_count": len(results.runs),
        "runs": results.runs,
    }, ensure_ascii=False, indent=2))


@app.command("prune-tracking-db")
def prune_tracking_db_command(
    tracking_db_path: Annotated[Path, typer.Argument()],
    retention_days: Annotated[int, typer.Option("--retention-days", min=1)] = settings.tracking_retention_days,
    drop_stale_tracked_tokens_days: Annotated[int | None, typer.Option("--drop-stale-tracked-tokens-days", min=1)] = settings.tracking_drop_stale_tokens_days,
) -> None:
    summary = TrackingState(tracking_db_path).prune(
        retention_days=retention_days,
        drop_stale_tracked_tokens_days=drop_stale_tracked_tokens_days,
    )
    typer.echo(json.dumps(summary, ensure_ascii=False, indent=2))


@app.command("rescan-tracked-tokens")
def rescan_tracked_tokens_command(
    tracking_db_path: Annotated[Path, typer.Argument()],
) -> None:
    results = rescan_tracked_tokens(tracking_db_path=tracking_db_path)
    typer.echo(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    app()
