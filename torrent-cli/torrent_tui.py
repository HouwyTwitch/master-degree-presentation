#!/usr/bin/env python3
"""torrent-tui — interactive TUI torrent client (download only, no seeding)."""

import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import libtorrent as lt
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Header, Input, Label, ListItem, ListView, Static


# ── helpers ───────────────────────────────────────────────────────────────────

_STATE_LABELS = {
    lt.torrent_status.queued_for_checking:  "queued",
    lt.torrent_status.checking_files:       "checking",
    lt.torrent_status.downloading_metadata: "metadata",
    lt.torrent_status.downloading:          "downloading",
    lt.torrent_status.finished:             "finished",
    lt.torrent_status.seeding:              "seeding",
    lt.torrent_status.allocating:           "allocating",
    lt.torrent_status.checking_resume_data: "resuming",
}


def _fmt(n: int) -> str:
    for u in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} PB"


def _bar(p: float, w: int = 32) -> str:
    f = int(p * w)
    return "█" * f + "░" * (w - f)


# ── torrent state snapshot ────────────────────────────────────────────────────

@dataclass
class TInfo:
    handle: object
    name: str = "Resolving…"
    progress: float = 0.0
    down: int = 0
    up: int = 0
    state: str = "connecting"
    done: int = 0
    total: int = 0
    peers: int = 0
    seeds: int = 0
    error: str = ""
    finished: bool = False
    save_path: str = ""


# ── background manager ────────────────────────────────────────────────────────

class Manager:
    DEFAULT_PATH = str(Path("~/Downloads").expanduser())

    def __init__(self):
        self._lock = threading.Lock()
        self._ses = self._make_session()
        self._torrents: dict[str, TInfo] = {}
        self._order: list[str] = []
        self._alive = True
        threading.Thread(target=self._loop, daemon=True).start()

    def _make_session(self) -> lt.session:
        ses = lt.session({
            "alert_mask": (
                lt.alert.category_t.status_notification
                | lt.alert.category_t.error_notification
            ),
            "upload_rate_limit": 0,
            "seed_time_limit": 0,
            "share_ratio_limit": 0,
            "seed_time_ratio_limit": 0,
            "active_seeds": 0,
        })
        for ext in ("ut_metadata", "ut_pex", "smart_ban"):
            ses.add_extension(ext)
        return ses

    def add(self, source: str, save_path: str = "") -> str:
        sp = str(Path(save_path or self.DEFAULT_PATH).expanduser().resolve())
        Path(sp).mkdir(parents=True, exist_ok=True)

        if source.startswith("magnet:"):
            p = lt.parse_magnet_uri(source)
            p.save_path = sp
        else:
            p = lt.add_torrent_params()
            p.ti = lt.torrent_info(source)
            p.save_path = sp

        h = self._ses.add_torrent(p)
        tid = "t" + uuid.uuid4().hex[:7]

        with self._lock:
            self._torrents[tid] = TInfo(handle=h, save_path=sp)
            self._order.append(tid)

        return tid

    def remove(self, tid: str):
        with self._lock:
            if tid in self._torrents:
                self._ses.remove_torrent(self._torrents[tid].handle)
                del self._torrents[tid]
                self._order.remove(tid)

    def snapshot(self) -> list[tuple[str, TInfo]]:
        with self._lock:
            return [(t, self._torrents[t]) for t in self._order if t in self._torrents]

    def _loop(self):
        while self._alive:
            with self._lock:
                for info in self._torrents.values():
                    h = info.handle
                    if not h.is_valid():
                        continue
                    try:
                        s = h.status()
                        n = h.name()
                        if n:
                            info.name = n
                        info.progress = s.progress
                        info.down     = s.download_rate
                        info.up       = s.upload_rate
                        info.state    = _STATE_LABELS.get(s.state, str(s.state))
                        info.done     = s.total_done
                        info.total    = s.total_wanted
                        info.peers    = s.num_peers
                        info.seeds    = s.num_seeds
                        ec = s.error_code
                        info.error    = ec.message() if ec.value() else ""
                        info.finished = s.state in (
                            lt.torrent_status.finished,
                            lt.torrent_status.seeding,
                        )
                    except Exception:
                        pass
            time.sleep(0.5)

    def stop(self):
        self._alive = False


# ── add-torrent modal ─────────────────────────────────────────────────────────

class AddModal(ModalScreen):
    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]

    DEFAULT_CSS = """
    AddModal {
        align: center middle;
    }
    #dlg {
        background: $surface;
        border: double $accent;
        padding: 2 4;
        width: 74;
        height: auto;
    }
    #dlg-title {
        text-align: center;
        color: $accent;
        text-style: bold;
        margin-bottom: 1;
    }
    .hint {
        color: $text-muted;
        margin-bottom: 0;
    }
    Input {
        margin-bottom: 1;
    }
    #btns {
        align: center middle;
        height: 3;
        margin-top: 1;
    }
    Button {
        margin: 0 1;
        min-width: 12;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="dlg"):
            yield Label("Add Torrent", id="dlg-title")
            yield Label("Magnet link or path to .torrent file:", classes="hint")
            yield Input(
                placeholder="magnet:?xt=urn:btih:…   or   /path/to/file.torrent",
                id="inp-src",
            )
            yield Label(
                f"Save directory  (blank = {Manager.DEFAULT_PATH}):",
                classes="hint",
            )
            yield Input(placeholder=Manager.DEFAULT_PATH, id="inp-path")
            with Horizontal(id="btns"):
                yield Button("Add", variant="primary", id="btn-ok")
                yield Button("Cancel", id="btn-cancel")

    def on_mount(self):
        self.query_one("#inp-src", Input).focus()

    @on(Input.Submitted, "#inp-src")
    def _tab_to_path(self, _):
        self.query_one("#inp-path", Input).focus()

    @on(Input.Submitted, "#inp-path")
    def _path_enter(self, _):
        self._submit()

    @on(Button.Pressed, "#btn-ok")
    def _ok(self):
        self._submit()

    @on(Button.Pressed, "#btn-cancel")
    def _cancel(self):
        self.dismiss(None)

    def _submit(self):
        src  = self.query_one("#inp-src",  Input).value.strip()
        path = self.query_one("#inp-path", Input).value.strip()
        self.dismiss((src, path) if src else None)


# ── detail panel (bottom bar) ─────────────────────────────────────────────────

class DetailBar(Static):
    DEFAULT_CSS = """
    DetailBar {
        height: 1;
        background: $panel;
        color: $text-muted;
        padding: 0 2;
    }
    """


# ── main app ──────────────────────────────────────────────────────────────────

class TorrentTUI(App):
    TITLE     = "torrent-tui"
    SUB_TITLE = "download-only BitTorrent client"

    DEFAULT_CSS = """
    Screen {
        background: $background;
        layers: base overlay;
    }

    #empty {
        height: 1fr;
        content-align: center middle;
        color: $text-muted;
        text-style: italic;
    }

    #list {
        height: 1fr;
        margin: 0 1;
        border: solid $panel-lighten-1;
        background: $surface;
        display: none;
    }

    ListItem {
        padding: 1 2;
        border-bottom: solid $panel-lighten-1;
        background: $surface;
        color: $text;
        height: 5;
    }

    ListItem.--highlight {
        background: $boost;
        border-left: thick $accent;
        padding-left: 1;
    }

    Footer {
        background: $panel;
        color: $text-muted;
    }

    Header {
        background: $panel;
    }
    """

    BINDINGS = [
        Binding("a",     "add",    "Add"),
        Binding("d",     "delete", "Delete"),
        Binding("q",     "quit",   "Quit"),
        Binding("j",     "down",   "↓",    show=False),
        Binding("k",     "up",     "↑",    show=False),
        Binding("up",    "up",     "Up",   show=False),
        Binding("down",  "down",   "Down", show=False),
    ]

    def __init__(self):
        super().__init__()
        self.mgr = Manager()

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(
            "No active torrents.\n\n"
            "[bold]a[/bold] — add magnet link or .torrent file\n"
            "[bold]d[/bold] — remove selected   [bold]q[/bold] — quit",
            id="empty",
        )
        yield ListView(id="list")
        yield DetailBar("", id="detail")
        yield Footer()

    def on_mount(self):
        self.set_interval(0.5, self._tick)

    # ── refresh loop ──────────────────────────────────────────────────────────

    def _tick(self):
        snap = self.mgr.snapshot()
        lv     = self.query_one("#list", ListView)
        empty  = self.query_one("#empty", Static)
        detail = self.query_one("#detail", DetailBar)

        if not snap:
            lv.display    = False
            empty.display = True
            detail.update("")
            return

        lv.display    = True
        empty.display = False

        snap_ids = [tid for tid, _ in snap]
        present  = {item.name for item in lv.children if isinstance(item, ListItem)}

        # remove items no longer tracked
        for item in list(lv.children):
            if isinstance(item, ListItem) and item.name not in snap_ids:
                item.remove()

        # append newly added items
        for tid, _ in snap:
            if tid not in present:
                lv.append(ListItem(Static("", id=f"c-{tid}"), name=tid, id=f"li-{tid}"))

        # update all content cells
        for tid, info in snap:
            try:
                self.query_one(f"#c-{tid}", Static).update(self._render_row(info))
            except Exception:
                pass

        # update detail bar for highlighted item
        highlighted = lv.highlighted_child
        if highlighted and isinstance(highlighted, ListItem):
            selected = next((i for t, i in snap if t == highlighted.name), None)
            if selected:
                detail.update(self._render_detail(selected))

    def _render_row(self, t: TInfo) -> str:
        name = t.name or "Resolving metadata…"
        bar  = _bar(t.progress)
        pct  = f"{t.progress * 100:.1f}%"

        if t.error:
            tag = f"[bold red]{t.error[:50]}[/bold red]"
        elif t.finished:
            tag = "[bold green]✓ complete[/bold green]"
        else:
            tag = f"[yellow]{t.state}[/yellow]"

        sz = f"  {_fmt(t.done)} / {_fmt(t.total)}" if t.total else ""

        stats = (
            f"  [cyan]↓[/cyan] {_fmt(t.down)}/s"
            f"  [red]↑[/red] {_fmt(t.up)}/s"
            f"  peers [bold]{t.peers}[/bold]"
            f"  seeds [bold]{t.seeds}[/bold]"
            f"{sz}"
        )
        return (
            f"[bold]{name}[/bold]   {tag}\n"
            f"  [green]{bar}[/green]  [bold]{pct}[/bold]\n"
            f"[dim]{stats}[/dim]"
        )

    def _render_detail(self, t: TInfo) -> str:
        sp = t.save_path or Manager.DEFAULT_PATH
        return (
            f" Save path: [cyan]{sp}[/cyan]"
            f"  │  {_fmt(t.done)} / {_fmt(t.total)}"
            f"  │  ↓ {_fmt(t.down)}/s  ↑ {_fmt(t.up)}/s"
            f"  │  peers {t.peers}  seeds {t.seeds}"
        )

    # ── key actions ───────────────────────────────────────────────────────────

    def action_add(self):
        def _got(result: Optional[tuple[str, str]]):
            if not result:
                return
            src, path = result
            try:
                self.mgr.add(src, path)
            except Exception as e:
                self.notify(str(e), title="Error adding torrent", severity="error")

        self.push_screen(AddModal(), _got)

    def action_delete(self):
        lv   = self.query_one("#list", ListView)
        item = lv.highlighted_child
        if item and isinstance(item, ListItem):
            self.mgr.remove(item.name)
            self.notify("Torrent removed (files kept).", title="Removed")

    def action_down(self):
        self.query_one("#list", ListView).action_cursor_down()

    def action_up(self):
        self.query_one("#list", ListView).action_cursor_up()

    def action_quit(self):
        self.mgr.stop()
        self.exit()


if __name__ == "__main__":
    TorrentTUI().run()
