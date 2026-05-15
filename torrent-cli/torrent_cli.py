#!/usr/bin/env python3
"""
torrent-cli — download-only BitTorrent client with magnet link support.
"""

import os
import sys
import time
import signal
import hashlib
import tempfile
from pathlib import Path

import click
import libtorrent as lt
from rich.console import Console
from rich.progress import (
    Progress,
    BarColumn,
    DownloadColumn,
    TransferSpeedColumn,
    TimeRemainingColumn,
    TextColumn,
    TaskID,
)
from rich.table import Table
from rich.live import Live
from rich.panel import Panel
from rich import print as rprint

console = Console()

# States where we have enough metadata to start downloading
DOWNLOADING_STATES = {
    lt.torrent_status.downloading,
    lt.torrent_status.downloading_metadata,
    lt.torrent_status.finished,
    lt.torrent_status.seeding,
    lt.torrent_status.checking_files,
    lt.torrent_status.checking_resume_data,
    lt.torrent_status.allocating,
}

STATE_LABELS = {
    lt.torrent_status.queued_for_checking: "queued",
    lt.torrent_status.checking_files: "checking",
    lt.torrent_status.downloading_metadata: "metadata",
    lt.torrent_status.downloading: "downloading",
    lt.torrent_status.finished: "finished",
    lt.torrent_status.seeding: "seeding",
    lt.torrent_status.allocating: "allocating",
    lt.torrent_status.checking_resume_data: "resuming",
}


def _fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _is_magnet(source: str) -> bool:
    return source.startswith("magnet:")


def _build_session(max_connections: int, max_upload_rate: int) -> lt.session:
    settings = {
        "alert_mask": (
            lt.alert.category_t.status_notification
            | lt.alert.category_t.error_notification
            | lt.alert.category_t.storage_notification
        ),
        "max_out_request_queue": 1500,
        "upload_rate_limit": max_upload_rate * 1024 if max_upload_rate > 0 else 0,
        "connections_limit": max_connections,
        # Disable seeding: stop immediately after download is complete
        "seed_time_limit": 0,
        "share_ratio_limit": 0,
        "seed_time_ratio_limit": 0,
        "active_seeds": 0,
    }
    ses = lt.session(settings)
    ses.add_extension("ut_metadata")
    ses.add_extension("ut_pex")
    ses.add_extension("smart_ban")
    return ses


def _add_torrent(
    ses: lt.session,
    source: str,
    save_path: str,
    sequential: bool,
) -> lt.torrent_handle:
    params = lt.add_torrent_params()
    params.save_path = save_path
    params.flags &= ~lt.torrent_flags.auto_managed  # we manage lifecycle ourselves

    if _is_magnet(source):
        params = lt.parse_magnet_uri(source)
        params.save_path = save_path
        params.flags &= ~lt.torrent_flags.auto_managed
    else:
        ti = lt.torrent_info(source)
        params.ti = ti

    handle = ses.add_torrent(params)

    if sequential:
        handle.set_sequential_download(True)

    return handle


def _make_info_table(status: lt.torrent_status, name: str) -> Table:
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold cyan", justify="right")
    table.add_column()

    state = STATE_LABELS.get(status.state, str(status.state))
    table.add_row("Name", name or "resolving…")
    table.add_row("State", state)
    table.add_row("Progress", f"{status.progress * 100:.2f}%")
    table.add_row(
        "Downloaded",
        f"{_fmt_size(status.total_done)} / {_fmt_size(status.total_wanted)}",
    )
    table.add_row("Down speed", f"{_fmt_size(status.download_rate)}/s")
    table.add_row("Up speed", f"{_fmt_size(status.upload_rate)}/s")
    table.add_row("Peers", str(status.num_peers))
    table.add_row("Seeds", str(status.num_seeds))
    return table


def _download(
    source: str,
    output_dir: str,
    sequential: bool,
    max_connections: int,
    max_upload_kbps: int,
    check_interval: float,
) -> int:
    """Core download loop. Returns 0 on success, 1 on error."""
    save_path = str(Path(output_dir).expanduser().resolve())
    Path(save_path).mkdir(parents=True, exist_ok=True)

    ses = _build_session(max_connections, max_upload_kbps)
    handle = _add_torrent(ses, source, save_path, sequential)

    done = False
    interrupted = False

    def _sigint(sig, frame):
        nonlocal interrupted
        interrupted = True

    signal.signal(signal.SIGINT, _sigint)
    signal.signal(signal.SIGTERM, _sigint)

    with Live(console=console, refresh_per_second=4, transient=True) as live:
        while not done and not interrupted:
            time.sleep(check_interval)

            status = handle.status()
            name = handle.name() or (status.name if hasattr(status, "name") else "")

            panel = Panel(
                _make_info_table(status, name),
                title="[bold green]torrent-cli[/bold green]",
                border_style="green",
            )
            live.update(panel)

            if status.state in (lt.torrent_status.finished, lt.torrent_status.seeding):
                done = True
            elif status.error_code.value() != 0:
                console.print(f"[red]Error:[/red] {status.error_code.message()}")
                ses.remove_torrent(handle)
                return 1

    if interrupted:
        console.print("\n[yellow]Interrupted — removing torrent (files kept).[/yellow]")
        ses.remove_torrent(handle)
        return 130

    # Stop seeding: remove torrent without deleting files
    ses.remove_torrent(handle)
    console.print(
        f"\n[bold green]Done![/bold green] Files saved to [cyan]{save_path}[/cyan]"
    )
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def cli():
    """torrent-cli — a download-only BitTorrent client.\n
    Supports .torrent files and magnet links.
    After the download finishes the torrent is removed (no seeding).
    """


@cli.command("download", short_help="Download a torrent or magnet link.")
@click.argument("source")
@click.option(
    "-o", "--output", "output_dir",
    default="./downloads",
    show_default=True,
    help="Directory to save files into.",
)
@click.option(
    "--sequential", is_flag=True, default=False,
    help="Download pieces in order (useful for media previews).",
)
@click.option(
    "--max-connections", default=200, show_default=True,
    help="Maximum peer connections.",
)
@click.option(
    "--max-upload", "max_upload_kbps", default=0, show_default=True,
    help="Max upload speed in KB/s (0 = no limit, but seeding is disabled anyway).",
)
@click.option(
    "--interval", "check_interval", default=0.5, show_default=True,
    help="Status refresh interval in seconds.",
)
def download_cmd(
    source: str,
    output_dir: str,
    sequential: bool,
    max_connections: int,
    max_upload_kbps: int,
    check_interval: float,
):
    """Download SOURCE (a .torrent file path or magnet link).\n
    Examples:\n
      torrent-cli download ubuntu.torrent\n
      torrent-cli download "magnet:?xt=urn:btih:…"\n
      torrent-cli download ubuntu.torrent -o ~/Downloads
    """
    if not _is_magnet(source) and not os.path.isfile(source):
        raise click.BadParameter(
            f"'{source}' is not a magnet link and the file does not exist.",
            param_hint="SOURCE",
        )

    kind = "magnet link" if _is_magnet(source) else f".torrent file [cyan]{source}[/cyan]"
    rprint(f"[bold]Starting download[/bold] from {kind}")
    rprint(f"  Output dir : [cyan]{output_dir}[/cyan]")
    rprint(f"  Sequential : {'yes' if sequential else 'no'}")
    rprint()

    rc = _download(
        source=source,
        output_dir=output_dir,
        sequential=sequential,
        max_connections=max_connections,
        max_upload_kbps=max_upload_kbps,
        check_interval=check_interval,
    )
    sys.exit(rc)


@cli.command("info", short_help="Show metadata from a .torrent file.")
@click.argument("torrent_file", type=click.Path(exists=True))
def info_cmd(torrent_file: str):
    """Print metadata for a local .torrent file without downloading."""
    ti = lt.torrent_info(torrent_file)

    table = Table(title=f"[bold]{ti.name()}[/bold]", show_header=True, header_style="bold magenta")
    table.add_column("Field", style="cyan", no_wrap=True)
    table.add_column("Value")

    size = ti.total_size()
    table.add_row("Name", ti.name())
    table.add_row("Total size", _fmt_size(size))
    table.add_row("Num files", str(ti.num_files()))
    table.add_row("Num pieces", str(ti.num_pieces()))
    table.add_row("Piece length", _fmt_size(ti.piece_length()))
    table.add_row("Info hash", str(ti.info_hashes().v1 if ti.info_hashes().has_v1() else ti.info_hashes().v2))
    table.add_row("Comment", ti.comment() or "—")
    table.add_row("Creator", ti.creator() or "—")

    console.print(table)

    files = ti.files()
    if ti.num_files() > 1:
        ftable = Table(title="Files", show_header=True, header_style="bold blue", min_width=60)
        ftable.add_column("#", justify="right", style="dim")
        ftable.add_column("Path")
        ftable.add_column("Size", justify="right")
        for i in range(ti.num_files()):
            ftable.add_row(str(i), files.file_path(i), _fmt_size(files.file_size(i)))
        console.print(ftable)


if __name__ == "__main__":
    cli()
