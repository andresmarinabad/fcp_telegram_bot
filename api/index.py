from __future__ import annotations

from dataclasses import asdict
from functools import lru_cache

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from bs4 import BeautifulSoup

from fcp.fecapa_client import (
    TeamInfo,
    _norm_name,
    _parse_match_rows,
    competition_map,
    fetch_calendar_html,
    fetch_classification_html,
    get_competitions,
    get_list_html,
    parse_standing_tables_with_headers,
    parse_teams_for_idc,
)

TARGET_COMPETITION = "PREBENJAMI PLATA BARCELONA"
TARGET_GROUP = "BCN PREBENJAMI PLATA 1"
TARGET_TEAM = "CPI SANT IGNASI B"


app = FastAPI(title="FCP Web API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


def _contains_target_group(html: str) -> bool:
    text = " ".join(BeautifulSoup(html, "html.parser").stripped_strings)
    return TARGET_GROUP.upper() in text.upper()


@lru_cache(maxsize=1)
def resolve_target() -> tuple[str, TeamInfo]:
    """
    Resolveix automàticament els IDs FECAPA a partir dels noms humans.

    No guardem cap idc/team_id al repositori perquè aquests IDs poden canviar.
    Primer filtrem la competició exacta, després l'equip exacte i finalment
    validem el grup exacte contra la classificació FECAPA.
    """
    competitions = [
        (cid, name)
        for cid, name in get_competitions()
        if _norm_name(name) == _norm_name(TARGET_COMPETITION)
    ]
    if not competitions:
        raise HTTPException(
            status_code=503,
            detail=f"No s'ha trobat la competició FECAPA: {TARGET_COMPETITION}",
        )

    list_html = get_list_html()
    candidates: list[tuple[str, TeamInfo]] = []

    for cid, _ in competitions:
        teams = parse_teams_for_idc(list_html, cid)
        for team in teams:
            if _norm_name(team.name) == _norm_name(TARGET_TEAM):
                candidates.append((cid, team))

    if not candidates:
        raise HTTPException(
            status_code=503,
            detail=(
                f"No s'ha trobat {TARGET_TEAM} dins de {TARGET_COMPETITION}. "
                f"Grup esperat: {TARGET_GROUP}."
            ),
        )

    validated: list[tuple[str, TeamInfo]] = []
    for cid, team in candidates:
        classification = fetch_classification_html(cid)
        if _contains_target_group(classification):
            tables = parse_standing_tables_with_headers(classification)
            if any(
                any(_norm_name(row.name) == _norm_name(TARGET_TEAM) for row in rows)
                for _, rows in tables
            ):
                validated.append((cid, team))

    if len(validated) == 1:
        return validated[0]

    if len(validated) > 1:
        raise HTTPException(
            status_code=503,
            detail=(
                f"Hi ha més d'una coincidència per {TARGET_TEAM} a "
                f"{TARGET_GROUP}; no s'ha escollit cap ID automàticament."
            ),
        )

    # Si el portal no inclou el nom del grup al HTML de classificació, però
    # només existeix una coincidència de competició + equip, és inequívoca.
    if len(candidates) == 1:
        return candidates[0]

    raise HTTPException(
        status_code=503,
        detail=(
            f"No s'ha pogut validar el grup {TARGET_GROUP} per {TARGET_TEAM}. "
            "FECAPA ha retornat diverses coincidències."
        ),
    )


def configured_target() -> tuple[str, TeamInfo]:
    try:
        return resolve_target()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Error resolent FECAPA: {exc}") from exc


@app.get("/api/team")
def team():
    cid, team_info = configured_target()
    competitions = competition_map()
    return {
        "competition_id": cid,
        "competition": competitions.get(cid, TARGET_COMPETITION),
        "group": TARGET_GROUP,
        "team": asdict(team_info),
    }


@app.get("/api/matches")
def matches():
    cid, team_info = configured_target()
    rows = _parse_match_rows(
        fetch_calendar_html(cid),
        team_sidgad_id=team_info.team_id,
    )
    return {
        "competition_id": cid,
        "competition": TARGET_COMPETITION,
        "group": TARGET_GROUP,
        "team": asdict(team_info),
        "matches": [
            {
                "jornada": row.jornada,
                "date": row.gamedate,
                "date_display": row.date_display,
                "time": row.time_display,
                "home": row.home,
                "away": row.away,
                "score": row.score or None,
                "played": bool(row.score),
            }
            for row in rows
        ],
    }


@app.get("/api/standings")
def standings():
    cid, team_info = configured_target()
    candidates = []

    for headers, rows in parse_standing_tables_with_headers(
        fetch_classification_html(cid)
    ):
        if any(_norm_name(row.name) == _norm_name(team_info.name) for row in rows):
            candidates.append((headers, rows))

    if not candidates:
        return {"headers": ["Pos", "Equip", "Pts"], "rows": []}

    headers, rows = candidates[0]
    return {
        "headers": ["Pos", "Equip", *headers],
        "rows": [
            {
                "position": row.pos,
                "team": row.name,
                "stats": row.stats,
            }
            for row in rows
        ],
    }
