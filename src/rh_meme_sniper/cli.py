from __future__ import annotations

import json
from pathlib import Path

from typing_extensions import Annotated

import typer

from rh_meme_sniper.pipeline import run_alert_loop_from_payload, run_live_alert_loop, run_query_pack, run_discovery
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
) -> None:
    try:
        results = run_query_pack(
            query_pack_path=query_pack_path,
            send_telegram=send_telegram,
            state_db_path=state_db_path,
            alert_cooldown_seconds=alert_cooldown_seconds,
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
) -> None:
    try:
        results = run_discovery(
            watchlist_path=watchlist_path,
            query_buckets_path=query_buckets_path,
            send_telegram=send_telegram,
            actor_id=actor_id,
            state_db_path=state_db_path,
            alert_cooldown_seconds=alert_cooldown_seconds,
        )
    except (ApifyAccessError, TwitterAPIIOAccessError) as exc:
        typer.echo(str(exc))
        raise typer.Exit(2) from exc

    typer.echo(json.dumps({
        "run_count": len(results.runs),
        "runs": results.runs,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    app()
