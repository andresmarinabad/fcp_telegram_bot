"""Client HTTP i parsing per al portal Sidgad / FECAPA hoquei patins."""

from __future__ import annotations

import html as html_module
import re
import threading
import time
from dataclasses import dataclass

import requests
from bs4 import BeautifulSoup

BASE = "https://www.server2.sidgad.es/fecapa"
ORIGIN = "https://www.hoqueipatins.fecapa.cat"

DEFAULT_HEADERS = {
    "Accept": "text/html, */*; q=0.01",
    "Accept-Language": "ca-ES,ca;q=0.9,es;q=0.8",
    "Origin": ORIGIN,
    "Referer": f"{ORIGIN}/",
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
}

LIST_URL = f"{BASE}/fecapa_ls_1.php"
MC_URL = f"{BASE}/fecapa_mc_1.php"

_CACHE_LOCK = threading.Lock()
_COMPETITIONS: list[tuple[str, str]] | None = None
_LIST_HTML: str | None = None
_CACHE_TS = 0.0
CACHE_TTL_SEC = 3600

_HTML_TTL = 300.0
_HTML_CACHE_LOCK = threading.Lock()
_HTML_CACHE: dict[tuple[str, str], tuple[float, str]] = {}


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(DEFAULT_HEADERS)
    return s


def _cache_get(key: tuple[str, str]) -> str | None:
    now = time.monotonic()
    with _HTML_CACHE_LOCK:
        hit = _HTML_CACHE.get(key)
        if not hit:
            return None
        ts, html = hit
        if (now - ts) > _HTML_TTL:
            del _HTML_CACHE[key]
            return None
        return html


def _cache_set(key: tuple[str, str], html: str) -> None:
    with _HTML_CACHE_LOCK:
        _HTML_CACHE[key] = (time.monotonic(), html)


def fetch_main_scorer_html() -> str:
    """Contingut del marcador / resum (equivalent a app2.py)."""
    with _session() as s:
        r = s.post(MC_URL, timeout=60)
        r.raise_for_status()
        return r.text


def _parse_competition_rows(html: str) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[tuple[str, str]] = []
    for a in soup.select("a.idc_master.listado_competiciones_fila"):
        i = a.get("id")
        name = (a.get("idc_name") or a.get("name") or "").strip()
        if i and name and i.isdigit():
            out.append((i, name))
    return out


def get_competitions(*, force_refresh: bool = False) -> list[tuple[str, str]]:
    """Llista (idc, nom) de totes les competicions (equivalent a carregar el llistat de la web)."""
    global _COMPETITIONS, _LIST_HTML, _CACHE_TS
    now = time.monotonic()
    with _CACHE_LOCK:
        if (
            not force_refresh
            and _COMPETITIONS is not None
            and _LIST_HTML is not None
            and (now - _CACHE_TS) < CACHE_TTL_SEC
        ):
            return list(_COMPETITIONS)
    with _session() as s:
        r = s.get(LIST_URL, timeout=120)
        r.raise_for_status()
        rows = _parse_competition_rows(r.text)
        list_html = r.text
    with _CACHE_LOCK:
        _COMPETITIONS = rows
        _LIST_HTML = list_html
        _CACHE_TS = time.monotonic()
    return list(rows)


def get_list_html(*, force_refresh: bool = False) -> str:
    """HTML del llistat de competicions (per extreure equips per idc)."""
    global _LIST_HTML
    get_competitions(force_refresh=force_refresh)
    with _CACHE_LOCK:
        return _LIST_HTML or ""


def competition_map(*, force_refresh: bool = False) -> dict[str, str]:
    return {i: n for i, n in get_competitions(force_refresh=force_refresh)}


@dataclass(frozen=True)
class TeamInfo:
    team_id: str
    abbr: str
    name: str


def parse_teams_for_idc(list_html: str, idc: str) -> list[TeamInfo]:
    soup = BeautifulSoup(list_html, "html.parser")
    inp = soup.find("input", id=f"teams_array_{idc}")
    if not inp:
        return []
    raw = (inp.get("value") or "").strip()
    if not raw:
        return []
    teams: list[TeamInfo] = []
    for part in raw.split(";"):
        part = part.strip()
        if not part:
            continue
        bits = [b.strip() for b in part.split(",")]
        if len(bits) >= 4 and bits[1].isdigit():
            teams.append(TeamInfo(team_id=bits[1], abbr=bits[2], name=bits[3]))
    return teams


def fetch_calendar_html(idc: str, *, site_lang: str = "ca") -> str:
    """Calendari / partits per idc (equivalent a app.py)."""
    key = ("cal", idc)
    hit = _cache_get(key)
    if hit is not None:
        return hit
    url = f"{BASE}/fecapa_cal_idc_{idc}_1.php"
    headers = {
        **DEFAULT_HEADERS,
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    }
    with _session() as s:
        r = s.post(url, headers=headers, data={"idc": idc, "site_lang": site_lang}, timeout=60)
        r.raise_for_status()
        text = r.text
    _cache_set(key, text)
    return text


def fetch_classification_html(idc: str, *, site_lang: str = "ca", idm: str = "1") -> str:
    key = ("clas", idc)
    hit = _cache_get(key)
    if hit is not None:
        return hit
    url = f"{BASE}/fecapa_clasif_idc_{idc}_1.php"
    headers = {
        **DEFAULT_HEADERS,
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    }
    with _session() as s:
        r = s.post(
            url,
            headers=headers,
            data={"idc": idc, "site_lang": site_lang, "idm": idm},
            timeout=60,
        )
        r.raise_for_status()
        text = r.text
    _cache_set(key, text)
    return text


@dataclass
class MatchRow:
    jornada: str
    gamedate: str
    date_display: str
    time_display: str
    home: str
    away: str
    score: str

    @property
    def sort_key(self) -> tuple[str, str]:
        return (self.gamedate or "00000000", self.time_display or "00:00")


_SCORE_RE = re.compile(r"^\s*(\d+)\s*[-–:]\s*(\d+)\s*$")


def _parse_match_rows(
    html: str, *, team_sidgad_id: str | None = None
) -> list[MatchRow]:
    soup = BeautifulSoup(html, "html.parser")
    rows: list[MatchRow] = []
    need = f"team_{team_sidgad_id}" if team_sidgad_id else None
    for tr in soup.select("tr.team_class"):
        classes = tr.get("class") or []
        if need and need not in classes:
            continue
        thead = tr.find_previous("thead")
        jornada = ""
        if thead:
            th = thead.find("th")
            if th:
                jornada = th.get_text(strip=True)
        gd = (tr.get("gamedate") or "").strip()
        less = tr.select("td.tabla_standard_less")
        date_d = less[0].get_text(strip=True) if len(less) > 0 else ""
        time_d = less[1].get_text(strip=True) if len(less) > 1 else ""
        teams = [d.get_text(strip=True) for d in tr.select("div.nombre_junto_logo")]
        home = teams[0] if len(teams) > 0 else ""
        away = teams[1] if len(teams) > 1 else ""
        if not (home.strip() and away.strip()):
            continue
        score = ""
        for td in tr.select("td.web_link_td"):
            t = td.get_text(strip=True)
            if _SCORE_RE.match(t):
                score = t.replace("–", "-")
                break
        rows.append(
            MatchRow(
                jornada=jornada,
                gamedate=gd,
                date_display=date_d,
                time_display=time_d,
                home=home,
                away=away,
                score=score,
            )
        )
    rows.sort(key=lambda m: m.sort_key)
    return rows


def _norm_name(s: str) -> str:
    return " ".join(s.strip().upper().split())


def match_involves_team(m: MatchRow, team: TeamInfo) -> bool:
    nh, na = _norm_name(m.home), _norm_name(m.away)
    nt = _norm_name(team.name)
    if nh == nt or na == nt:
        return True
    ab = _norm_name(team.abbr)
    if len(ab) >= 2 and (ab == nh or ab == na):
        return True
    return False


def format_match_line(m: MatchRow, *, team: TeamInfo | None = None) -> str:
    esc = html_module.escape
    loc = ""
    if team:
        if _norm_name(m.home) == _norm_name(team.name):
            loc = " (local)"
        elif _norm_name(m.away) == _norm_name(team.name):
            loc = " (visitant)"
    if m.score:
        return (
            f"• {esc(m.jornada)} — {esc(m.date_display)} {esc(m.time_display)}{loc}\n"
            f"  {esc(m.home)} <b>{esc(m.score)}</b> {esc(m.away)}"
        )
    return (
        f"• {esc(m.jornada)} — {esc(m.date_display)} {esc(m.time_display)}{loc}\n"
        f"  {esc(m.home)} vs {esc(m.away)}"
    )


def format_team_calendar_list(
    html: str, comp_title: str, team: TeamInfo, *, limit: int = 25
) -> str:
    rows = _parse_match_rows(html, team_sidgad_id=team.team_id)
    pending = [m for m in rows if not m.score]
    if not pending:
        return (
            f"<b>{html_module.escape(comp_title)}</b>\n"
            f"<b>{html_module.escape(team.name)}</b>\n\n"
            "No hi ha partits pendents (sense resultat) per aquest equip, "
            "o no s'han pogut llegir del calendari."
        )
    lines = [
        f"<b>{html_module.escape(comp_title)}</b>",
        f"<b>{html_module.escape(team.name)}</b> — només partits pendents\n",
    ]
    for m in pending[:limit]:
        lines.append(format_match_line(m, team=team))
    if len(pending) > limit:
        lines.append(f"\n<i>… i {len(pending) - limit} partits més pendents</i>")
    out = "\n".join(lines)
    if len(out) > 3900:
        out = out[:3890] + "\n…"
    return out


@dataclass
class StandingRow:
    pos: str
    name: str
    stats: list[str]


# Etiquetes curtes coherents amb la web (lang_ca)
_CA_HEADER_MAP: dict[str, str] = {
    "punts": "Pts",
    "j": "J",
    "g": "G",
    "e": "E",
    "p": "P",
    "f": "F",
    "c": "C",
    "gav": "G+",
    "pen": "Pen",
}

_DEFAULT_STAT_HEADERS = ["Pts", "J", "G", "E", "P", "F", "C", "G+", "Pen"]


def _header_label_from_th(th) -> str:
    for sel in ("span.lang_ca", "span.lang_es", "span.lang_en"):
        sp = th.select_one(sel)
        if sp:
            t = " ".join(sp.get_text().split()).strip()
            if not t:
                continue
            key = t.lower().rstrip(".")
            if key in _CA_HEADER_MAP:
                return _CA_HEADER_MAP[key]
            if len(t) <= 4:
                return t.upper()
            return t[:4].upper()
    raw = " ".join(th.get_text().split()).strip()
    return raw[:5] if raw else "?"


def _parse_stat_headers_from_table(table) -> list[str]:
    thead = table.find("thead")
    if not thead:
        return []
    labels: list[str] = []
    for th in thead.find_all("th"):
        cs = th.get("colspan")
        if cs is not None:
            try:
                if int(cs) >= 3:
                    continue
            except ValueError:
                pass
        lab = _header_label_from_th(th)
        if lab and lab != "?":
            labels.append(lab)
    return labels


def _parse_standing_rows_from_table(table) -> list[StandingRow]:
    body_rows: list[StandingRow] = []
    for tr in table.select("tbody tr"):
        tds = tr.find_all("td")
        if len(tds) < 4:
            continue
        pos = tds[0].get_text(strip=True)
        if not pos.isdigit():
            continue
        name_el = tds[2].select_one(".no_mobile") or tds[2]
        name = name_el.get_text(strip=True)
        stats = [td.get_text(strip=True) for td in tds[3:]]
        body_rows.append(StandingRow(pos=pos, name=name, stats=stats))
    return body_rows


def parse_classification_tables(html: str) -> list[list[StandingRow]]:
    """Compatibilitat: només files, sense capçaleres."""
    return [rows for _, rows in parse_standing_tables_with_headers(html)]


def parse_standing_tables_with_headers(
    html: str,
) -> list[tuple[list[str], list[StandingRow]]]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[tuple[list[str], list[StandingRow]]] = []
    for table in soup.select("table.tabla_standard"):
        rows = _parse_standing_rows_from_table(table)
        if not rows:
            continue
        hdrs = _parse_stat_headers_from_table(table)
        n_stats = max(len(r.stats) for r in rows)
        if len(hdrs) < n_stats:
            for i in range(len(hdrs), n_stats):
                hdrs.append(
                    _DEFAULT_STAT_HEADERS[i]
                    if i < len(_DEFAULT_STAT_HEADERS)
                    else f"{i + 1}"
                )
        elif len(hdrs) > n_stats:
            hdrs = hdrs[:n_stats]
        out.append((hdrs, rows))
    return out


def _clip_team_name(name: str, max_w: int) -> str:
    name = " ".join(name.split())
    if len(name) <= max_w:
        return name
    return name[: max_w - 1] + "…"


def _norm_stat_cell(s: str) -> str:
    return (s or "").replace("\u00a0", " ").replace("\u2007", " ").strip()


def _is_unsigned_int_text(s: str) -> bool:
    t = _norm_stat_cell(s)
    return bool(t) and t.isdigit()


def _standing_table_is_real_standings(rows: list[StandingRow]) -> bool:
    """
    Només taules amb dades de classificació reals.

    A la web hi ha taules amb posició i nom però sense punts ni altres números;
    aquí exigim que **cada fila** tingui punts (1a estadística) i partits jugats
    (2a estadística), com a la taula oficial FECAPA.
    """
    if not rows:
        return False
    for r in rows:
        if len(r.stats) < 2:
            return False
        if not _is_unsigned_int_text(r.stats[0]) or not _is_unsigned_int_text(
            r.stats[1]
        ):
            return False
    return True


def _find_team_row_index(rows: list[StandingRow], team: TeamInfo) -> int | None:
    for i, r in enumerate(rows):
        if _norm_name(r.name) == _norm_name(team.name):
            return i
    for i, r in enumerate(rows):
        if _norm_name(team.abbr) == _norm_name(r.name):
            return i
    return None


def _standing_table_has_any_positive_points(rows: list[StandingRow]) -> bool:
    """Exclou taules on tots els equips tenen 0 punts (plantilla / grup sense dades)."""
    for r in rows:
        if not r.stats:
            continue
        t = _norm_stat_cell(r.stats[0])
        if t.isdigit() and int(t) > 0:
            return True
    return False


def _team_points_at_row(rows: list[StandingRow], hit_idx: int) -> int:
    if hit_idx < 0 or hit_idx >= len(rows) or not rows[hit_idx].stats:
        return -1
    t = _norm_stat_cell(rows[hit_idx].stats[0])
    return int(t) if t.isdigit() else -1


def _ascii_standings_pos_name_pts(
    rows: list[StandingRow],
    *,
    highlight_row_offsets: set[int],
    name_max: int = 30,
) -> str:
    """Taula mínima: posició, equip, punts (primera estadística = punts FECAPA)."""
    if not rows:
        return ""
    pos_cells: list[str] = []
    name_cells: list[str] = []
    pts_cells: list[str] = []
    for i, r in enumerate(rows):
        if i in highlight_row_offsets:
            pos_cells.append(f"►{r.pos}")
        else:
            pos_cells.append(r.pos)
        name_cells.append(_clip_team_name(r.name, name_max))
        pts = (r.stats[0].strip() if r.stats else "") or "—"
        pts_cells.append(pts)

    w_pos = max(len("Pos"), max(len(p) for p in pos_cells))
    w_name = max(len("Equip"), max(len(n) for n in name_cells))
    w_pts = max(len("Pts"), max(len(p) for p in pts_cells))
    widths = [w_pos, w_name, w_pts]

    def hline() -> str:
        return "+" + "+".join("-" * (w + 2) for w in widths) + "+"

    lines = [hline()]
    head = [
        "Pos".center(w_pos),
        "Equip".ljust(w_name),
        "Pts".rjust(w_pts),
    ]
    lines.append("| " + " | ".join(head) + " |")
    lines.append(hline())
    for i in range(len(rows)):
        row = [
            pos_cells[i].rjust(w_pos),
            name_cells[i].ljust(w_name),
            pts_cells[i].rjust(w_pts),
        ]
        lines.append("| " + " | ".join(row) + " |")
    lines.append(hline())
    return "\n".join(lines)


def _standings_pre_chunks(
    rows: list[StandingRow],
    hit_idx: int,
    *,
    name_max: int = 30,
    max_rows_per_chunk: int = 24,
) -> list[str]:
    """Genera un o més taules ASCII; es parte per fila si cal per límit de Telegram."""
    if not rows:
        return []
    chunks: list[str] = []
    for start in range(0, len(rows), max_rows_per_chunk):
        end = min(len(rows), start + max_rows_per_chunk)
        sub = rows[start:end]
        local_hi: set[int] = set()
        if start <= hit_idx < end:
            local_hi.add(hit_idx - start)
        chunks.append(
            _ascii_standings_pos_name_pts(
                sub, highlight_row_offsets=local_hi, name_max=name_max
            )
        )
    return chunks


def format_standings_for_team(
    html: str, comp_title: str, team: TeamInfo
) -> str:
    tables = parse_standing_tables_with_headers(html)
    if not tables:
        return (
            f"<b>{html_module.escape(comp_title)}</b>\n\n"
            "No s'ha pogut llegir la classificació."
        )
    esc = html_module.escape

    candidates: list[tuple[list[StandingRow], int]] = []
    for _hdrs, rows in tables:
        if not _standing_table_is_real_standings(rows):
            continue
        if not _standing_table_has_any_positive_points(rows):
            continue
        hit_idx = _find_team_row_index(rows, team)
        if hit_idx is None:
            continue
        candidates.append((rows, hit_idx))

    blocks: list[str] = [
        f"<b>{esc(comp_title)}</b>",
        f"<b>{esc(team.name)}</b>",
        "",
    ]
    if not candidates:
        blocks.append(
            "<i>Cap classificació vàlida on surti aquest equip "
            "(es descarten taules sense punts o sense dades completes).</i>"
        )
    else:
        # Una sola taula: si n'hi ha diverses (p. ex. grups), la que dona més punts al teu equip.
        best_rows, hit_idx = max(
            candidates,
            key=lambda pair: _team_points_at_row(pair[0], pair[1]),
        )
        pre_chunks = _standings_pre_chunks(best_rows, hit_idx, name_max=30)
        blocks.append(
            "<i>Classificació completa: posició, equip i punts.</i>"
        )
        blocks.append("")
        blocks.append("<b>Classificació</b>")
        nch = len(pre_chunks)
        for i, table_txt in enumerate(pre_chunks):
            if nch > 1:
                blocks.append(f"<i>Part {i + 1}/{nch}</i>")
            blocks.append(f"<pre>{esc(table_txt)}</pre>")
        blocks.append("<i>► = el teu equip</i>")

    out = "\n".join(blocks).strip()
    if len(out) > 4090 and candidates:
        # Missatge massa llarg: tornar a generar amb menys equips per <pre> o noms més curts.
        for nm, mr in ((26, 18), (22, 14), (18, 10)):
            pre2 = _standings_pre_chunks(
                best_rows, hit_idx, name_max=nm, max_rows_per_chunk=mr
            )
            blocks2: list[str] = [
                f"<b>{esc(comp_title)}</b>",
                f"<b>{esc(team.name)}</b>",
                "",
                "<i>Classificació completa (partida en diversos blocs si cal).</i>",
                "",
                "<b>Classificació</b>",
            ]
            nc = len(pre2)
            for i, ttxt in enumerate(pre2):
                if nc > 1:
                    blocks2.append(f"<i>Part {i + 1}/{nc}</i>")
                blocks2.append(f"<pre>{esc(ttxt)}</pre>")
            blocks2.append("<i>► = el teu equip</i>")
            cand = "\n".join(blocks2).strip()
            if len(cand) <= 4090:
                out = cand
                break
        else:
            out = cand[:4075] + "\n…"
    return out


def format_calendar_summary(
    html: str, title: str, *, max_upcoming: int = 12, max_results: int = 18
) -> str:
    matches = _parse_match_rows(html)
    t = html_module.escape(title)
    if not matches:
        return f"<b>{t}</b>\n\nNo s'han trobat partits en aquest calendari."

    played = [m for m in matches if m.score]
    pending = [m for m in matches if not m.score]

    lines: list[str] = [f"<b>{t}</b>", ""]
    if pending:
        lines.append("<b>Partits sense resultat (pendents / sense marcador)</b>")
        for m in pending[:max_upcoming]:
            lines.append(
                "• "
                f"{html_module.escape(m.date_display)} {html_module.escape(m.time_display)} — "
                f"{html_module.escape(m.home)} vs {html_module.escape(m.away)}"
            )
        if len(pending) > max_upcoming:
            lines.append(f"… i {len(pending) - max_upcoming} més")
        lines.append("")
    if played:
        lines.append("<b>Resultats</b>")
        if len(played) > max_results:
            lines.append(
                f"<i>últims {max_results} de {len(played)}</i>"
            )
        chunk = played[-max_results:]
        for m in chunk:
            lines.append(
                "• "
                f"{html_module.escape(m.date_display)} "
                f"{html_module.escape(m.home)} "
                f"{html_module.escape(m.score)} "
                f"{html_module.escape(m.away)}"
            )

    out = "\n".join(lines).strip()
    if len(out) > 3900:
        out = out[:3890] + "\n…"
    return out


def _norm_jornada_key(s: str) -> str:
    return " ".join((s or "").split())


def format_team_last_jornada_html(
    cal_html: str, comp_title: str, team: TeamInfo
) -> str:
    """
    Tots els partits jugats (amb resultat) de l'última jornada en què ha participat l'equip.
    Si en una mateixa jornada hi ha més d'un partit, es mostren tots.
    """
    matches = _parse_match_rows(cal_html, team_sidgad_id=team.team_id)
    played = [m for m in matches if m.score]
    esc = html_module.escape
    if not played:
        return (
            f"<b>{esc(comp_title)}</b>\n<b>{esc(team.name)}</b>\n\n"
            "No hi ha cap partit jugat registrat."
        )
    anchor = played[-1]
    jk = _norm_jornada_key(anchor.jornada)
    if jk:
        same_round = [m for m in played if _norm_jornada_key(m.jornada) == jk]
    else:
        gd = anchor.gamedate
        same_round = [m for m in played if m.gamedate == gd]
    if not same_round:
        same_round = [anchor]
    same_round.sort(key=lambda m: m.sort_key)
    title_line = esc(jk) if jk else f"Data {esc(anchor.date_display)}"
    lines = [
        f"<b>{esc(comp_title)}</b>",
        f"<b>{esc(team.name)}</b>\n",
        "<b>Última jornada disputada</b>",
        f"<i>{title_line}</i>\n",
    ]
    for m in same_round:
        lines.append(format_match_line(m, team=team))
    out = "\n".join(lines)
    if len(out) > 3900:
        out = out[:3890] + "\n…"
    return out


def format_team_next_match_html(
    cal_html: str, comp_title: str, team: TeamInfo
) -> str:
    matches = _parse_match_rows(cal_html, team_sidgad_id=team.team_id)
    pending = [m for m in matches if not m.score]
    next_m = pending[0] if pending else None
    esc = html_module.escape
    if not next_m:
        return (
            f"<b>{esc(comp_title)}</b>\n<b>{esc(team.name)}</b>\n\n"
            "No hi ha cap partit pendent amb data."
        )
    return (
        f"<b>{esc(comp_title)}</b>\n<b>{esc(team.name)}</b>\n\n"
        "<b>Pròxim partit</b>\n"
        + format_match_line(next_m, team=team)
    )


def filter_competitions(
    competitions: list[tuple[str, str]], query: str, *, limit: int = 20
) -> list[tuple[str, str]]:
    q = query.strip().lower()
    if not q:
        return []
    return [(i, n) for i, n in competitions if q in n.lower()][:limit]


def invalidate_html_cache_for_idc(idc: str) -> None:
    with _HTML_CACHE_LOCK:
        for k in list(_HTML_CACHE):
            if k[1] == idc:
                del _HTML_CACHE[k]
