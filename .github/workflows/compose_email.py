import os, requests, datetime, sys, time
from jinja2 import Template

# ---------------------------
# Utilities & configuration
# ---------------------------

def fail(msg):
    print(f"[ERROR] {msg}", file=sys.stderr)
    sys.exit(1)

# Required env vars
for req in ["LEAGUE_ID", "ESPN_S2", "SWID"]:
    if not os.environ.get(req):
        fail(f"Missing env var {req}. Add it under GitHub Secrets.")

LEAGUE_ID = os.environ["LEAGUE_ID"]
SEASON = os.environ.get("SEASON")
WEEK = os.environ.get("WEEK")
ESPN_S2 = os.environ["ESPN_S2"]
SWID = os.environ["SWID"]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://fantasy.espn.com",
    "Referer": f"https://fantasy.espn.com/football/league?leagueId={LEAGUE_ID}",
    "Connection": "keep-alive",
    # Extra headers some ESPN edges expect:
    "x-fantasy-source": "kona",
    "x-fantasy-platform": "kona-PROD",
}

def _try_fetch(session, url, params, cookies):
    r = session.get(url, params=params, cookies=cookies, timeout=30)
    ct = r.headers.get("Content-Type", "").lower()
    print(f"[INFO] HTTP {r.status_code}  content-type={ct}  url={r.url}")
    if "application/json" not in ct:
        snippet = (r.text or "")[:300].replace("\n", " ")
        print(f"[WARN] Non-JSON response snippet: {snippet}", file=sys.stderr)
    return r

# ---------------------------
# ESPN data fetching
# ---------------------------

def espn_get_scoreboard(league_id, season, week):
    cookies = {"espn_s2": ESPN_S2, "SWID": SWID}
    params = {"view": "mMatchupScore", "scoringPeriodId": str(week)}

    s = requests.Session()
    s.headers.update(HEADERS)

    hosts = [
        f"https://fantasy.espn.com/apis/v3/games/ffl/seasons/{season}/segments/0/leagues/{league_id}",
        f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{season}/segments/0/leagues/{league_id}",
    ]

    for host in hosts:
        print(f"[INFO] Trying host: {host}")
        for i in range(3):
            r = _try_fetch(s, host, params, cookies)
            if r.status_code == 200 and r.headers.get("Content-Type", "").lower().startswith("application/json"):
                data = r.json()

                # If teams missing, fetch mTeam and merge
                if "teams" not in data or not data.get("teams"):
                    print("[INFO] 'teams' missing; fetching mTeam view…")
                    r2 = _try_fetch(s, host, {"view": "mTeam"}, cookies)
                    if r2.status_code == 200 and r2.headers.get("Content-Type", "").lower().startswith("application/json"):
                        data2 = r2.json()
                        data["teams"] = data2.get("teams", [])
                    else:
                        print("[WARN] Could not fetch mTeam; proceeding without names.", file=sys.stderr)

                # Ensure schedule present
                if "schedule" not in data or not data.get("schedule"):
                    print("[INFO] 'schedule' missing; refetching mMatchupScore…")
                    r3 = _try_fetch(s, host, {"view": "mMatchupScore", "scoringPeriodId": str(week)}, cookies)
                    if r3.status_code == 200 and r3.headers.get("Content-Type", "").lower().startswith("application/json"):
                        data3 = r3.json()
                        data["schedule"] = data3.get("schedule", data.get("schedule", []))

                return data

            if r.status_code in (403, 429) or not r.headers.get("Content-Type", "").lower().startswith("application/json"):
                time.sleep(2 + i)
                continue

            print(f"[ERROR] ESPN HTTP {r.status_code}: {(r.text or '')[:300]}", file=sys.stderr)
            r.raise_for_status()

    fail("ESPN kept returning non-JSON or blocked responses. Refresh ESPN_S2 and SWID (keep braces {}) and try again.")

# ---------------------------
# Transformations
# ---------------------------

def summarize_matchups(data, week):
    teams_raw = data.get("teams", []) or []

    def team_name(t: dict) -> str:
        loc = (t.get("location") or "").strip()
        nick = (t.get("nickname") or "").strip()
        nm = (t.get("name") or "").strip()
        abbr = (t.get("abbrev") or "").strip()
        tid = t.get("id")
        if loc or nick:
            return f"{loc} {nick}".strip()
        if nm:
            return nm
        if abbr:
            return abbr
        return f"Team {tid}" if tid is not None else "Team"

    teams = {}
    for t in teams_raw:
        tid = t.get("id")
        if tid is not None:
            teams[tid] = team_name(t)

    print(f"[INFO] Built team name map for {len(teams)} teams")

    matchups = []
    for m in data.get("schedule", []):
        if m.get("matchupPeriodId") != int(week):
            continue
        if "away" not in m or "home" not in m:
            continue

        home_id = m["home"]["teamId"]
        away_id = m["away"]["teamId"]
        home_pts = float(m["home"].get("totalPoints", 0) or 0)
        away_pts = float(m["away"].get("totalPoints", 0) or 0)

        # Determine winner
        winner_flag = (m.get("winner") or "UNDECIDED").upper()
        if winner_flag == "HOME" and home_pts != away_pts:
            winner = "home"
        elif winner_flag == "AWAY" and home_pts != away_pts:
            winner = "away"
        elif home_pts > away_pts:
            winner = "home"
        elif away_pts > home_pts:
            winner = "away"
        else:
            winner = "tie" if home_pts == away_pts and winner_flag != "UNDECIDED" else "undecided"

        margin = abs(home_pts - away_pts)

        matchups.append({
            "home": teams.get(home_id, f"Team {home_id}"),
            "away": teams.get(away_id, f"Team {away_id}"),
            "home_pts": round(home_pts, 2),
            "away_pts": round(away_pts, 2),
            "winner": winner,           # 'home' | 'away' | 'tie' | 'undecided'
            "margin": round(margin, 2),
        })

    return matchups

def extract_standings(data):
    """
    Best-effort standings table if the teams payload includes record info.
    Returns list of dicts: {name, wins, losses, ties, points_for}
    """
    rows = []
    for t in data.get("teams", []):
        name = f"{t.get('location','Team')} {t.get('nickname','')}".strip() or t.get("name") or t.get("abbrev") or "Team"
        rec = (t.get("record") or {}).get("overall") or {}
        wins = rec.get("wins")
        losses = rec.get("losses")
        ties = rec.get("ties")
        pf = t.get("points", {}).get("scored") or t.get("valuesByStat", {}).get("0")  # stat 0 often points
        if wins is not None and losses is not None:
            rows.append({
                "name": name,
                "wins": int(wins),
                "losses": int(losses),
                "ties": int(ties or 0),
                "points_for": round(float(pf or 0), 2),
            })
    rows.sort(key=lambda r: (r["wins"], r["points_for"]), reverse=True)
    return rows

def build_narrative(matchups, week):
    if not matchups:
        return f"No results yet for Week {week}."

    closest = min(matchups, key=lambda m: m["margin"])
    blowout = max(matchups, key=lambda m: m["margin"])
    lowest = min(matchups, key=lambda m: min(m["home_pts"], m["away_pts"]))

    lines = []
    lines.append(f"Week {week} is in the books!")

    # Closest game
    lines.append(
        f" The closest battle was between {closest['away']} and {closest['home']}, "
        f"decided by just {closest['margin']} points."
    )

    # Biggest blowout
    lines.append(
        f" Meanwhile, {blowout['home']} vs {blowout['away']} was a blowout "
        f"with a margin of {blowout['margin']}."
    )

    # Lowest score smack talk
    loser_team = lowest["home"] if lowest["home_pts"] < lowest["away_pts"] else lowest["away"]
    loser_score = min(lowest["home_pts"], lowest["away_pts"])
    lines.append(
        f" And let’s not forget: {loser_team} posted a week-low {loser_score} points. "
        f"Maybe try setting a lineup next time? 😉"
    )

    return " ".join(lines)

# ---------------------------
# HTML template
# ---------------------------

HTML_TMPL = Template("""
<!doctype html>
<html>
  <body style="margin:0; padding:0; background:#f5f7fb;">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background:#f5f7fb; padding:24px 0;">
      <tr>
        <td align="center">
          <table role="presentation" width="640" cellspacing="0" cellpadding="0" border="0" style="width:640px; max-width:100%; background:#ffffff; border-radius:12px; overflow:hidden; box-shadow:0 2px 8px rgba(0,0,0,0.06);">
            <!-- Header -->
            <tr>
              <td style="background:#0f172a; color:#ffffff; padding:24px 28px; font-family:Arial, Helvetica, sans-serif;">
                <div style="font-size:22px; font-weight:700; letter-spacing:.3px;">Fantasy Weekly</div>
                <div style="margin-top:6px; font-size:13px; opacity:.9;">Week {{ week }} • Generated {{ now }}</div>
              </td>
            </tr>

            <!-- Narrative -->
            <tr>
              <td style="padding:20px 24px; font-family:Arial, Helvetica, sans-serif;">
                <div style="font-size:16px; font-weight:700; color:#0f172a; margin-bottom:10px;">Weekly Recap</div>
                <div style="font-size:14px; color:#334155; line-height:1.5;">
                  {{ narrative }}
                </div>
              </td>
            </tr>

            <!-- Matchups -->
            <tr>
              <td style="padding:12px 24px 6px 24px; font-family:Arial, Helvetica, sans-serif;">
                <div style="font-size:16px; font-weight:700; color:#0f172a; margin-bottom:10px;">Matchups & Results</div>
                <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="border-collapse:separate; border-spacing:0 10px;">
                  {% for m in matchups %}
                  <tr>
                    <td style="background:#f8fafc; border:1px solid #e5e7eb; border-radius:10px; padding:12px;">
                      <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0">
                        <tr>
                          <td style="width:40%; font-size:14px; color:#0f172a; {% if m.winner=='away' %}font-weight:700{% endif %}">{{ m.away }}</td>
                          <td style="width:20%; text-align:center; font-size:14px; color:#334155;">
                            <span style="display:inline-block; background:#eef2ff; color:#3730a3; border-radius:999px; padding:3px 10px; font-size:12px;">
                              {{ m.away_pts }} — {{ m.home_pts }}
                            </span>
                          </td>
                          <td style="width:40%; font-size:14px; color:#0f172a; text-align:right; {% if m.winner=='home' %}font-weight:700{% endif %}">{{ m.home }}</td>
                        </tr>
                        <tr>
                          <td colspan="3" style="padding-top:6px; font-size:12px; color:#64748b; text-align:center;">
                            {% if m.winner=='home' %}
                              <strong style="color:#065f46;">Winner: {{ m.home }}</strong> (margin {{ m.margin }})
                            {% elif m.winner=='away' %}
                              <strong style="color:#065f46;">Winner: {{ m.away }}</strong> (margin {{ m.margin }})
                            {% elif m.winner=='tie' %}
                              <strong style="color:#7c3aed;">Result: Tie</strong>
                            {% else %}
                              <em>In progress</em>
                            {% endif %}
                          </td>
                        </tr>
                      </table>
                    </td>
                  </tr>
                  {% endfor %}
                </table>
                {% if matchups|length == 0 %}
                  <div style="font-size:13px; color:#64748b; padding:6px 0 12px;">No matchups found for this week yet.</div>
                {% endif %}
              </td>
            </tr>

            <!-- Standings (optional) -->
            {% if standings and standings|length > 0 %}
            <tr>
              <td style="padding:4px 24px 20px 24px; font-family:Arial, Helvetica, sans-serif;">
                <div style="font-size:16px; font-weight:700; color:#0f172a; margin:14px 0 8px;">Standings</div>
                <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse; border:1px solid #e5e7eb;">
                  <thead>
                    <tr style="background:#f1f5f9;">
                      <th align="left" style="padding:8px 10px; font-size:12px; color:#334155; border-bottom:1px solid #e5e7eb;">Team</th>
                      <th align="center" style="padding:8px 10px; font-size:12px; color:#334155; border-bottom:1px solid #e5e7eb;">W-L-T</th>
                      <th align="right" style="padding:8px 10px; font-size:12px; color:#334155; border-bottom:1px solid #e5e7eb;">PF</th>
                    </tr>
                  </thead>
                  <tbody>
                    {% for r in standings %}
                    <tr>
                      <td style="padding:8px 10px; font-size:13px; color:#0f172a; border-bottom:1px solid #e5e7eb;">{{ r.name }}</td>
                      <td align="center" style="padding:8px 10px; font-size:13px; color:#334155; border-bottom:1px solid #e5e7eb;">
                        {{ r.wins }}-{{ r.losses }}{% if r.ties %}-{{ r.ties }}{% endif %}
                      </td>
                      <td align="right" style="padding:8px 10px; font-size:13px; color:#334155; border-bottom:1px solid #e5e7eb;">{{ r.points_for }}</td>
                    </tr>
                    {% endfor %}
                  </tbody>
                </table>
              </td>
            </tr>
            {% endif %}

            <!-- Footer -->
            <tr>
              <td style="padding:18px 24px 26px 24px; font-family:Arial, Helvetica, sans-serif; color:#94a3b8; font-size:12px; text-align:center;">
                Sent by your friendly Fantasy Agent • Week {{ week }}
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>
""".strip())

# ---------------------------
# Main
# ---------------------------

def main():
    today = datetime.date.today()
    season = int(SEASON) if SEASON else today.year
    week = int(WEEK) if WEEK else 1

    data = espn_get_scoreboard(LEAGUE_ID, season, week)
    matchups = summarize_matchups(data, week)
    standings = extract_standings(data)
    narrative = build_narrative(matchups, week)

    if not matchups:
        print("[WARN] No matchups found for that week/season.", file=sys.stderr)

    html = HTML_TMPL.render(
        week=week,
        matchups=matchups,
        standings=standings,
        narrative=narrative,
        now=datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    )
    subject = f"Fantasy Week {week} Results & Notes"

    os.makedirs("out", exist_ok=True)
    with open("out/body.html", "w", encoding="utf-8") as f:
        f.write(html)
    with open("out/subject.txt", "w", encoding="utf-8") as f:
        f.write(subject)
    print("[INFO] Wrote out/body.html and out/subject.txt")

if __name__ == "__main__":
    main()
