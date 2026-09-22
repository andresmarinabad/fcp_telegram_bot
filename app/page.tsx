"use client";

import { useEffect, useState } from "react";

type Team = { team_id: string; abbr: string; name: string };
type Match = {
  jornada: string;
  date: string;
  date_display: string;
  time: string;
  home: string;
  away: string;
  score: string | null;
  played: boolean;
};
type TeamResponse = { competition: string; team: Team };
type MatchesResponse = { matches: Match[] };

export default function Home() {
  const [team, setTeam] = useState<TeamResponse | null>(null);
  const [matches, setMatches] = useState<Match[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    Promise.all([
      fetch("/api/team", { cache: "no-store" }).then(async r => {
        if (!r.ok) throw new Error(await r.text());
        return r.json() as Promise<TeamResponse>;
      }),
      fetch("/api/matches", { cache: "no-store" }).then(async r => {
        if (!r.ok) throw new Error(await r.text());
        return r.json() as Promise<MatchesResponse>;
      }),
    ])
      .then(([teamData, matchesData]) => {
        setTeam(teamData);
        setMatches(matchesData.matches);
      })
      .catch(e => setError(e instanceof Error ? e.message : "Error carregant les dades"))
      .finally(() => setLoading(false));
  }, []);

  const played = matches.filter(m => m.played).length;

  return (
    <main className="container">
      <header>
        <p className="eyebrow">FECAPA</p>
        <h1>{team?.team.name ?? "Calendari"}</h1>
        {team?.competition && <p className="competition">{team.competition}</p>}
      </header>

      {loading && <p className="status">Carregant...</p>}

      {error && (
        <section className="error">
          <strong>No s'han pogut carregar les dades.</strong>
          <p>{error}</p>
        </section>
      )}

      {!loading && !error && (
        <section>
          <div className="summary">
            <span>{matches.length} partits</span>
            <span>{played} amb resultat</span>
            <span>{matches.length - played} pendents</span>
          </div>

          <div className="table-wrap">
            <table>
              <thead>
                <tr><th>Jornada</th><th>Data</th><th>Partit</th><th>Resultat</th></tr>
              </thead>
              <tbody>
                {matches.map((match, index) => (
                  <tr key={`${match.date}-${match.time}-${index}`}>
                    <td className="round">{match.jornada || "—"}</td>
                    <td className="date">
                      <strong>{match.date_display || "—"}</strong>
                      {match.time && <span>{match.time}</span>}
                    </td>
                    <td className="match">
                      <span>{match.home}</span>
                      <span className="separator">—</span>
                      <span>{match.away}</span>
                    </td>
                    <td className="score">
                      {match.score ?? <span className="pending">Pendent</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {!matches.length && <p className="empty">No s'han trobat partits.</p>}
          </div>
        </section>
      )}

      <footer>Dades obtingudes de FECAPA · Actualització en carregar la pàgina</footer>
    </main>
  );
}
