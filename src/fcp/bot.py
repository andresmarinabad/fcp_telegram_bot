"""Bot de Telegram: competicions FECAPA, equips, classificació i favorits."""

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
from functools import partial
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, NetworkError, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    PicklePersistence,
)

from . import fecapa_client as fc

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# Timeouts HTTP (s): els defectes de la llibreria són baixos i fallen amb xarxa lenta
# o missatges grans (classificació, etc.).
TG_CONNECT_TIMEOUT = 45.0
TG_READ_TIMEOUT = 45.0
TG_WRITE_TIMEOUT = 45.0
TG_POOL_TIMEOUT = 45.0
# getUpdates fa long polling; el read ha ser major que timeout del poll (10s per defecte).
TG_GET_UPDATES_READ_TIMEOUT = 70.0

# Arrel del repositori (bot_persistence.pickle al mateix nivell que pyproject.toml).
ROOT = Path(__file__).resolve().parents[2]
PERSISTENCE_PATH = ROOT / "bot_persistence.pickle"

GITHUB_USER = "andresmarinabad"
GITHUB_URL = f"https://github.com/{GITHUB_USER}"

ABOUT_HTML = (
    "<b>Què és aquest bot?</b>\n"
    "Servei no oficial per consultar competicions, equips, classificació, "
    "calendaris i resultats d'<b>hoquei patins</b> de la FECAPA "
    "(dades del mateix portal que la web oficial).\n\n"
    "<b>Autor</b>\n"
    "Projecte personal fet per <b>Andrés Marín</b>.\n\n"
    "<b>Github</b>\n"
    f'<a href="{GITHUB_URL}">github.com/{GITHUB_USER}</a>'
)

PAGE_SIZE = 8
TEAM_PAGE = 8
FAV_KEY = "favorites"
MAX_FAV = 20

CALLBACK_PAGE = re.compile(r"^p:(\d+)$")
CALLBACK_REFRESH = re.compile(r"^r:(\d+)$")
CALLBACK_COMP = re.compile(r"^c:(\d+)$")
CALLBACK_TEAMS = re.compile(r"^e:(\d+):(\d+)$")
CALLBACK_TEAM = re.compile(r"^t:(\d+):(\d+)$")
CALLBACK_CLAS = re.compile(r"^s:(\d+):(\d+)$")
CALLBACK_LAST = re.compile(r"^u:(\d+):(\d+)$")
CALLBACK_NEXT = re.compile(r"^n:(\d+):(\d+)$")
CALLBACK_LIST = re.compile(r"^l:(\d+):(\d+)$")
CALLBACK_FAV = re.compile(r"^w:(\d+):(\d+)$")


def _names_map(context: ContextTypes.DEFAULT_TYPE) -> dict[str, str]:
    data = context.application.bot_data
    m = data.get("fecapa_names")
    if not isinstance(m, dict) or not m:
        comps = fc.get_competitions()
        m = {i: n for i, n in comps}
        data["fecapa_names"] = m
    return m


def _refresh_names(context: ContextTypes.DEFAULT_TYPE) -> dict[str, str]:
    comps = fc.get_competitions(force_refresh=True)
    m = {i: n for i, n in comps}
    context.application.bot_data["fecapa_names"] = m
    return m


def _truncate(s: str, max_len: int = 40) -> str:
    s = s.strip()
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "…"


def _favorites(context: ContextTypes.DEFAULT_TYPE) -> list[dict]:
    raw = context.user_data.setdefault(FAV_KEY, [])
    if not isinstance(raw, list):
        raw = []
        context.user_data[FAV_KEY] = raw
    return raw


def _is_favorite(context: ContextTypes.DEFAULT_TYPE, idc: str, team_id: str) -> bool:
    return any(
        f.get("idc") == idc and f.get("team_id") == team_id for f in _favorites(context)
    )


def _toggle_favorite(
    context: ContextTypes.DEFAULT_TYPE,
    idc: str,
    team_id: str,
    team_name: str,
    comp_name: str,
) -> bool:
    """Retorna True si ha quedat desat, False si s'ha eliminat."""
    favs = _favorites(context)
    for i, f in enumerate(favs):
        if f.get("idc") == idc and f.get("team_id") == team_id:
            favs.pop(i)
            return False
    if len(favs) >= MAX_FAV:
        favs.pop(0)
    favs.append(
        {
            "idc": idc,
            "team_id": team_id,
            "team_name": team_name,
            "comp_name": comp_name,
        }
    )
    return True


def _resolve_team(list_html: str, idc: str, team_id: str) -> fc.TeamInfo | None:
    for t in fc.parse_teams_for_idc(list_html, idc):
        if t.team_id == team_id:
            return t
    return None


def keyboard_competitions_page(
    competitions: list[tuple[str, str]], page: int
) -> InlineKeyboardMarkup:
    total = len(competitions)
    start = page * PAGE_SIZE
    chunk = competitions[start : start + PAGE_SIZE]
    rows: list[list[InlineKeyboardButton]] = []
    for i, name in chunk:
        rows.append(
            [InlineKeyboardButton(_truncate(name), callback_data=f"c:{i}")]
        )
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Anterior", callback_data=f"p:{page - 1}"))
    if start + PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("Següent ▶️", callback_data=f"p:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append(
        [
            InlineKeyboardButton(
                "🔄 Actualitzar llistat", callback_data=f"r:{page}"
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


def keyboard_search_results(items: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(_truncate(n), callback_data=f"c:{i}")]
        for i, n in items
    ]
    rows.append([InlineKeyboardButton("📋 Totes (paginades)", callback_data="p:0")])
    return InlineKeyboardMarkup(rows)


def keyboard_team_pages(idc: str, teams: list[fc.TeamInfo], page: int) -> InlineKeyboardMarkup:
    total = len(teams)
    start = page * TEAM_PAGE
    chunk = teams[start : start + TEAM_PAGE]
    rows: list[list[InlineKeyboardButton]] = []
    for t in chunk:
        label = _truncate(f"{t.name} ({t.abbr})")
        rows.append([InlineKeyboardButton(label, callback_data=f"t:{idc}:{t.team_id}")])
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"e:{idc}:{page - 1}"))
    if start + TEAM_PAGE < total:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"e:{idc}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append(
        [InlineKeyboardButton("📋 Competicions", callback_data="p:0")]
    )
    return InlineKeyboardMarkup(rows)


def keyboard_team_hub(
    idc: str, team_id: str, *, favorit: bool
) -> InlineKeyboardMarkup:
    fav_label = "🗑 Treure dels favorits" if favorit else "⭐️ Desar com a favorit"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📊 Classificació", callback_data=f"s:{idc}:{team_id}"),
                InlineKeyboardButton("🏁 Última jornada", callback_data=f"u:{idc}:{team_id}"),
            ],
            [
                InlineKeyboardButton("📅 Pròxim partit", callback_data=f"n:{idc}:{team_id}"),
            ],
            [
                InlineKeyboardButton(
                    "📆 Partits pendents (equip)",
                    callback_data=f"l:{idc}:{team_id}",
                )
            ],
            [
                InlineKeyboardButton(fav_label, callback_data=f"w:{idc}:{team_id}"),
            ],
            [
                InlineKeyboardButton("👥 Altres equips", callback_data=f"e:{idc}:0"),
                InlineKeyboardButton("📋 Competicions", callback_data="p:0"),
            ],
        ]
    )


def keyboard_back_hub(idc: str, team_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "⬅️ Menú equip", callback_data=f"t:{idc}:{team_id}"
                )
            ],
            [
                InlineKeyboardButton("👥 Equips", callback_data=f"e:{idc}:0"),
                InlineKeyboardButton("📋 Competicions", callback_data="p:0"),
            ],
        ]
    )


async def _run_blocking(fn, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, partial(fn, *args, **kwargs))


async def on_telegram_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    if isinstance(err, TimedOut):
        log.warning("Timeout parlant amb Telegram (reintenta o comprova la xarxa): %s", err)
        return
    if isinstance(err, NetworkError):
        log.warning("Error de xarxa amb Telegram: %s", err)
        return
    log.exception("Error no gestionat processant l'update", exc_info=err)


def _list_header(page: int, total: int) -> str:
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    return (
        f"<b>Competicions</b> (pàgina {page + 1}/{pages}, {total} en total)\n\n"
        "Toca una competició per triar equip i veure classificació, partits, etc."
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _run_blocking(_refresh_names, context)
    text = (
        "<b>🏑 FECAPA Hoquei patins</b>\n\n"
        "Comandes:\n"
        "• /llista — competicions\n"
        "• /cerca text — filtra competició\n"
        "• /favorits — accés ràpid als equips desats\n"
        "• /refresca — actualitza el llistat\n"
        "• /about — què és això i crèdits\n\n"
        "Després de triar una competició, escull un equip i navega pel menú."
    )
    if update.message:
        await update.message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "📋 Veure competicions", callback_data="p:0"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "⭐️ Els meus favorits", callback_data="fav:list"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "ℹ️ About", callback_data="about:show"
                        )
                    ],
                ]
            ),
        )


async def cmd_about(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(
            ABOUT_HTML,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )


async def on_about_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q or q.data != "about:show":
        return
    await q.answer()
    await q.message.reply_text(
        ABOUT_HTML,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def cmd_favorits(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    favs = _favorites(context)
    if not favs:
        if update.message:
            await update.message.reply_text(
                "Encara no tens favorits. Obre un equip i prem "
                "«Desar com a favorit»."
            )
        return
    rows = [
        [
            InlineKeyboardButton(
                _truncate(f["team_name"]),
                callback_data=f"t:{f['idc']}:{f['team_id']}",
            )
        ]
        for f in favs[-MAX_FAV:][::-1]
    ]
    if update.message:
        await update.message.reply_text(
            "<b>Favorits</b>\n\nToca un equip per obrir el seu menú.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(rows),
        )


async def cmd_llista(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    comps = await _run_blocking(fc.get_competitions)
    context.application.bot_data["fecapa_names"] = {i: n for i, n in comps}
    page = 0
    if context.args and context.args[0].isdigit():
        page = max(0, int(context.args[0]))
    max_page = max(0, (len(comps) - 1) // PAGE_SIZE)
    page = min(page, max_page)
    header = _list_header(page, len(comps))
    if update.message:
        await update.message.reply_text(
            header,
            parse_mode="HTML",
            reply_markup=keyboard_competitions_page(comps, page),
        )


async def cmd_refresca(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    n = len(await _run_blocking(_refresh_names, context))
    if update.message:
        await update.message.reply_text(f"Llistat actualitzat: {n} competicions.")


async def cmd_cerca(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        if update.message:
            await update.message.reply_text(
                "Ús: /cerca NACIONAL\n(o qualsevol tros del nom de la competició)"
            )
        return
    q = " ".join(context.args)
    comps = await _run_blocking(fc.get_competitions)
    context.application.bot_data["fecapa_names"] = {i: n for i, n in comps}
    found = fc.filter_competitions(comps, q, limit=20)
    if not found:
        if update.message:
            await update.message.reply_text(f"Cap competició coincideix amb «{q}».")
        return
    if update.message:
        await update.message.reply_text(
            f"Resultats per «{q}» ({len(found)}):",
            reply_markup=keyboard_search_results(found),
        )


async def on_refresh_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q:
        return
    await q.answer("Actualitzant…")
    comps = await _run_blocking(_refresh_names, context)
    page = 0
    m = CALLBACK_REFRESH.match(q.data or "")
    if m:
        page = int(m.group(1))
    max_page = max(0, (len(comps) - 1) // PAGE_SIZE)
    page = min(page, max_page)
    start = page * PAGE_SIZE
    if start >= len(comps):
        page = 0
    try:
        await q.edit_message_text(
            _list_header(page, len(comps)),
            parse_mode="HTML",
            reply_markup=keyboard_competitions_page(comps, page),
        )
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


async def on_page_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q or not q.data:
        return
    if CALLBACK_REFRESH.match(q.data):
        await on_refresh_cb(update, context)
        return
    m = CALLBACK_PAGE.match(q.data)
    if not m:
        return
    await q.answer()
    page = int(m.group(1))
    comps = await _run_blocking(fc.get_competitions)
    context.application.bot_data["fecapa_names"] = {i: n for i, n in comps}
    max_page = max(0, (len(comps) - 1) // PAGE_SIZE)
    page = min(page, max_page)
    start = page * PAGE_SIZE
    if start >= len(comps) and page > 0:
        page = 0
    try:
        await q.edit_message_text(
            _list_header(page, len(comps)),
            parse_mode="HTML",
            reply_markup=keyboard_competitions_page(comps, page),
        )
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


async def on_fav_list_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q or q.data != "fav:list":
        return
    await q.answer()
    favs = _favorites(context)
    if not favs:
        await q.edit_message_text(
            "No tens favorits desats.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("📋 Competicions", callback_data="p:0")]]
            ),
        )
        return
    rows = [
        [
            InlineKeyboardButton(
                _truncate(f["team_name"]),
                callback_data=f"t:{f['idc']}:{f['team_id']}",
            )
        ]
        for f in favs[-MAX_FAV:][::-1]
    ]
    rows.append([InlineKeyboardButton("📋 Competicions", callback_data="p:0")])
    await q.edit_message_text(
        "<b>Favorits</b>\n\nToca un equip.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def on_competition_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q:
        return
    m = CALLBACK_COMP.match(q.data or "")
    if not m:
        return
    idc = m.group(1)
    await q.answer()
    names = _names_map(context)
    title = names.get(idc, f"Competició {idc}")
    await q.edit_message_text(
        f"Carregant equips… <b>{html.escape(title)}</b>",
        parse_mode="HTML",
    )
    list_html = await _run_blocking(fc.get_list_html)
    teams = fc.parse_teams_for_idc(list_html, idc)
    if not teams:
        await q.edit_message_text(
            f"<b>{html.escape(title)}</b>\n\n"
            "No s'han trobat equips en aquesta competició.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("📋 Competicions", callback_data="p:0")]]
            ),
        )
        return
    await q.edit_message_text(
        f"<b>{html.escape(title)}</b>\n\n"
        f"Escull un equip ({len(teams)}):",
        parse_mode="HTML",
        reply_markup=keyboard_team_pages(idc, teams, 0),
    )


async def on_teams_page_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q:
        return
    m = CALLBACK_TEAMS.match(q.data or "")
    if not m:
        return
    idc, page_s = m.group(1), m.group(2)
    page = int(page_s)
    await q.answer()
    names = _names_map(context)
    title = names.get(idc, f"Competició {idc}")
    list_html = await _run_blocking(fc.get_list_html)
    teams = fc.parse_teams_for_idc(list_html, idc)
    if not teams:
        await q.edit_message_text(
            "Sense dades d'equips.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("📋 Competicions", callback_data="p:0")]]
            ),
        )
        return
    max_page = max(0, (len(teams) - 1) // TEAM_PAGE)
    page = min(page, max_page)
    await q.edit_message_text(
        f"<b>{html.escape(title)}</b>\n\n"
        f"Escull un equip ({len(teams)}):",
        parse_mode="HTML",
        reply_markup=keyboard_team_pages(idc, teams, page),
    )


async def on_team_hub_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q:
        return
    m = CALLBACK_TEAM.match(q.data or "")
    if not m:
        return
    idc, team_id = m.group(1), m.group(2)
    await q.answer()
    names = _names_map(context)
    comp_name = names.get(idc, f"Competició {idc}")
    list_html = await _run_blocking(fc.get_list_html)
    team = _resolve_team(list_html, idc, team_id)
    if not team:
        await q.edit_message_text(
            "No s'ha trobat l'equip.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("📋 Competicions", callback_data="p:0")]]
            ),
        )
        return
    fav = _is_favorite(context, idc, team_id)
    text = (
        f"<b>{html.escape(team.name)}</b> <i>({html.escape(team.abbr)})</i>\n"
        f"<i>{html.escape(comp_name)}</i>\n\n"
        "Tria la informació que vols veure:"
    )
    try:
        await q.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=keyboard_team_hub(idc, team_id, favorit=fav),
        )
    except BadRequest:
        await q.message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=keyboard_team_hub(idc, team_id, favorit=fav),
        )


async def on_fav_toggle_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q:
        return
    m = CALLBACK_FAV.match(q.data or "")
    if not m:
        return
    idc, team_id = m.group(1), m.group(2)
    names = _names_map(context)
    comp_name = names.get(idc, f"Competició {idc}")
    list_html = await _run_blocking(fc.get_list_html)
    team = _resolve_team(list_html, idc, team_id)
    if not team:
        await q.answer("Equip no trobat", show_alert=True)
        return
    saved = _toggle_favorite(context, idc, team_id, team.name, comp_name)
    await q.answer("Desat als favorits" if saved else "Eliminat dels favorits")
    fav = _is_favorite(context, idc, team_id)
    text = (
        f"<b>{html.escape(team.name)}</b> <i>({html.escape(team.abbr)})</i>\n"
        f"<i>{html.escape(comp_name)}</i>\n\n"
        "Tria la informació que vols veure:"
    )
    try:
        await q.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=keyboard_team_hub(idc, team_id, favorit=fav),
        )
    except BadRequest:
        pass


async def on_standings_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q:
        return
    m = CALLBACK_CLAS.match(q.data or "")
    if not m:
        return
    idc, team_id = m.group(1), m.group(2)
    await q.answer()
    names = _names_map(context)
    comp_name = names.get(idc, f"Competició {idc}")
    list_html = await _run_blocking(fc.get_list_html)
    team = _resolve_team(list_html, idc, team_id)
    if not team:
        return
    await q.edit_message_text("Carregant classificació…", parse_mode="HTML")
    try:
        clas_html = await _run_blocking(fc.fetch_classification_html, idc)
        body = await _run_blocking(
            fc.format_standings_for_team, clas_html, comp_name, team
        )
    except Exception as e:
        log.exception("clasif failed")
        body = f"Error: {html.escape(str(e))}"
    await q.edit_message_text(
        body,
        parse_mode="HTML",
        reply_markup=keyboard_back_hub(idc, team_id),
    )


async def on_last_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q:
        return
    m = CALLBACK_LAST.match(q.data or "")
    if not m:
        return
    idc, team_id = m.group(1), m.group(2)
    await q.answer()
    names = _names_map(context)
    comp_name = names.get(idc, f"Competició {idc}")
    list_html = await _run_blocking(fc.get_list_html)
    team = _resolve_team(list_html, idc, team_id)
    if not team:
        return
    await q.edit_message_text("Carregant última jornada…", parse_mode="HTML")
    try:
        cal_html = await _run_blocking(fc.fetch_calendar_html, idc)
        body = await _run_blocking(
            fc.format_team_last_jornada_html, cal_html, comp_name, team
        )
    except Exception as e:
        log.exception("last jornada")
        body = f"Error: {html.escape(str(e))}"
    await q.edit_message_text(
        body,
        parse_mode="HTML",
        reply_markup=keyboard_back_hub(idc, team_id),
    )


async def on_next_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q:
        return
    m = CALLBACK_NEXT.match(q.data or "")
    if not m:
        return
    idc, team_id = m.group(1), m.group(2)
    await q.answer()
    names = _names_map(context)
    comp_name = names.get(idc, f"Competició {idc}")
    list_html = await _run_blocking(fc.get_list_html)
    team = _resolve_team(list_html, idc, team_id)
    if not team:
        return
    await q.edit_message_text("Carregant…", parse_mode="HTML")
    try:
        cal_html = await _run_blocking(fc.fetch_calendar_html, idc)
        body = await _run_blocking(
            fc.format_team_next_match_html, cal_html, comp_name, team
        )
    except Exception as e:
        log.exception("next match")
        body = f"Error: {html.escape(str(e))}"
    await q.edit_message_text(
        body,
        parse_mode="HTML",
        reply_markup=keyboard_back_hub(idc, team_id),
    )


async def on_team_cal_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q:
        return
    m = CALLBACK_LIST.match(q.data or "")
    if not m:
        return
    idc, team_id = m.group(1), m.group(2)
    await q.answer()
    names = _names_map(context)
    comp_name = names.get(idc, f"Competició {idc}")
    list_html = await _run_blocking(fc.get_list_html)
    team = _resolve_team(list_html, idc, team_id)
    if not team:
        return
    await q.edit_message_text("Carregant partits pendents…", parse_mode="HTML")
    try:
        cal_html = await _run_blocking(fc.fetch_calendar_html, idc)
        body = await _run_blocking(
            fc.format_team_calendar_list, cal_html, comp_name, team
        )
    except Exception as e:
        log.exception("team cal")
        body = f"Error: {html.escape(str(e))}"
    await q.edit_message_text(
        body,
        parse_mode="HTML",
        reply_markup=keyboard_back_hub(idc, team_id),
    )


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit(
            "Defineix la variable d'entorn TELEGRAM_BOT_TOKEN (veure .env.example)."
        )
    persistence = PicklePersistence(filepath=str(PERSISTENCE_PATH))
    app = (
        Application.builder()
        .token(token)
        .persistence(persistence)
        .connect_timeout(TG_CONNECT_TIMEOUT)
        .read_timeout(TG_READ_TIMEOUT)
        .write_timeout(TG_WRITE_TIMEOUT)
        .pool_timeout(TG_POOL_TIMEOUT)
        .get_updates_connect_timeout(TG_CONNECT_TIMEOUT)
        .get_updates_read_timeout(TG_GET_UPDATES_READ_TIMEOUT)
        .get_updates_write_timeout(TG_WRITE_TIMEOUT)
        .get_updates_pool_timeout(TG_POOL_TIMEOUT)
        .build()
    )
    app.add_error_handler(on_telegram_error)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("about", cmd_about))
    app.add_handler(CommandHandler("info", cmd_about))
    app.add_handler(CallbackQueryHandler(on_about_cb, pattern=r"^about:show$"))
    app.add_handler(CommandHandler("llista", cmd_llista))
    app.add_handler(CommandHandler("cerca", cmd_cerca))
    app.add_handler(CommandHandler("refresca", cmd_refresca))
    app.add_handler(CommandHandler("favorits", cmd_favorits))
    app.add_handler(CommandHandler("favs", cmd_favorits))
    app.add_handler(CallbackQueryHandler(on_fav_list_cb, pattern=r"^fav:list$"))
    app.add_handler(CallbackQueryHandler(on_fav_toggle_cb, pattern=r"^w:\d+:\d+$"))
    app.add_handler(CallbackQueryHandler(on_standings_cb, pattern=r"^s:\d+:\d+$"))
    app.add_handler(CallbackQueryHandler(on_last_cb, pattern=r"^u:\d+:\d+$"))
    app.add_handler(CallbackQueryHandler(on_next_cb, pattern=r"^n:\d+:\d+$"))
    app.add_handler(CallbackQueryHandler(on_team_cal_cb, pattern=r"^l:\d+:\d+$"))
    app.add_handler(CallbackQueryHandler(on_team_hub_cb, pattern=r"^t:\d+:\d+$"))
    app.add_handler(CallbackQueryHandler(on_teams_page_cb, pattern=r"^e:\d+:\d+$"))
    app.add_handler(CallbackQueryHandler(on_competition_cb, pattern=r"^c:\d+$"))
    app.add_handler(CallbackQueryHandler(on_page_cb, pattern=r"^(p:\d+|r:\d+)$"))
    log.info("Bot en marxa (polling)")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        bootstrap_retries=5,
    )


if __name__ == "__main__":
    main()
