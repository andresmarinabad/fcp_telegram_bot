from __future__ import annotations

import os
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from fcp.fecapa_client import (
    TeamInfo,
    _parse_match_rows,
    competition_map,
    fetch_calendar_html,
    fetch_classification_html,
    parse_standing_tables_with_headers,
)

app = FastAPI(title="FCP Web API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"])


def configured_team() -> TeamInfo:
    team_id = os.getenv("FECAPA_TEAM_ID")
    team_name = os.getenv("FECAPA_TEAM_NAME")
    team_abbr = os.getenv("FECAPA_TEAM_ABBR", "")
    if not team_id or not team_name:
        raise HTTPException(status_code=500, detail="FECAPA_TEAM_ID and FECAPA_TEAM_NAME are not configured")
    return TeamInfo(team_id=team_id, abbr=team_abbr, name=team_name)


def competition_id() -> str:
    value = os.getenv("FECAPA_COMPETITION_ID")
    if not value:
        raise HTTPException(status_code=500, detail="FECAPA_COMPETITION_ID is not configured")
    return value


@app.get("/api/team")
def team():
    team_info = configured_team()
    cid = competition_id()
    competitions = competition_map()
    return {
        "competition_id": cid,
        "competition": competitions.get(cid, os.getenv("FECAPA_COMPETITION_NAME", "")),
        "team": asdict(team_info),
    }


@app.get("/api/matches")
def matches():
    team_info = configured_team()
    cid = competition_id()
    rows = _parse_match_rows(fetch_calendar_html(cid), team_sidgad_id=team_info.team_id)
    return {
        "competition_id": cid,
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
    team_info = configured_team()
    cid = competition_id()
    candidates = []
    for headers, rows in parse_standing_tables_with_headers(fetch_classification_html(cid)):
        for row in rows:
            if row.name.strip().upper() == team_info.name.strip().upper():
                candidates.append(rows)
                break
    if not candidates:
        return {"headers": ["Pos", "Equip", "Pts"], "rows": []}
    rows = candidates[0]
    return {
        "headers": ["Pos", "Equip", "Pts"],
        "rows": [
            {"position": row.pos, "team": row.name, "points": row.stats[0] if row.stats else ""}
            for row in rows
        ],
    }
