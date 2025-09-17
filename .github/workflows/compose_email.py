import os, requests, datetime, sys, time
from jinja2 import Template

# =========================
# Utilities & configuration
# =========================

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
    "x-fantasy-source": "kona",
    "x-fantasy-platform": "kona-PROD",
}

LINEUP_SLOT_BENCH = 20  # ESPN bench slot id
POS_QB = 0
POS_WR = 4  # ESPN Wide Receiver position id
POS_RB = 2
POS_DST = 16
POS_K = 17  # <-- used by Weekly Challenge rotation
POS_TE = 6              # Tight End position id
LINEUP_SLOT_FLEX = 23   # ESPN RB/WR/TE FLEX lineup slot id
LINEUP_SLOT_IR = 21  # injured reserve; not a starter

# Logo config (hosted URL)
LOGO_URL = os.environ.get("LOGO_URL")
if not LOGO_URL:
    # Auto-build a GitHub raw URL if running in Actions and file at assets/genosmith.PNG
    _repo = os.environ.get("GITHUB_REPOSITORY")  # "owner/repo"
    _branch = os.environ.get("GITHUB_REF_NAME", "main")
    _path = os.environ.get("LOGO_PATH", "assets/genosmith.PNG")
    if _repo:
        LOGO_URL = f"https://raw.githubusercontent.com/{_repo}/{_branch}/{_path}"

# ============
# HTTP helpers
# ============

def _try_fetch(session, url, params, cookies):
    r = session.get(url, params=params, cookies=cookies, timeout=30)
    ct = r.headers.get("Content-Type", "").lower()
    print(f"[INFO] HTTP {r.status_code}  content-type={ct}  url={r.url}")
    if "application/json" not in ct:
        snippet = (r.text or "")[:300].replace("\n", " ")
        print(f"[WARN] Non-JSON response snippet: {snippet}", file=sys.stderr)
    return r

# =====================
# ESPN data fetching
# =====================

def espn_fetch_jsons(league_id, season, week):
    """
    Returns a dict with keys:
      - scoreboard: schedule + maybe teams
      - teams: teams (ensured)
      - boxscore: schedule with roster entries for the scoring period, when available
    """
    cookies = {"espn_s2": ESPN_S2, "SWID": SWID}
    s = requests.Session()
    s.headers.update(HEADERS)

    hosts = [
        f"https://fantasy.espn.com/apis/v3/games/ffl/seasons/{season}/segments/0/leagues/{league_id}",
        f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{season}/segments/0/leagues/{league_id}",
    ]

    out = {"scoreboard": None, "teams": None, "boxscore": None}

    # 1) Scoreboard (mMatchupScore)
    for host in hosts:
        print(f"[INFO] Trying scoreboard host: {host}")
        for i in range(3):
            r = _try_fetch(s, host, {"view": "mMatchupScore", "scoringPeriodId": str(week)}, cookies)
            if r.status_code == 200 and r.headers.get("Content-Type","").lower().startswith("application/json"):
                out["scoreboard"] = r.json()
                break
            time.sleep(1 + i)
        if out["scoreboard"] is not None:
            break

    if out["scoreboard"] is None:
        fail("Could not fetch scoreboard JSON (mMatchupScore).")

    # 2) Teams (mTeam) if not present
    if "teams" not in out["scoreboard"] or not out["scoreboard"].get("teams"):
        for host in hosts:
            print(f"[INFO] Fetching teams via mTeam: {host}")
            r = _try_fetch(s, host, {"view": "mTeam"}, cookies)
            if r.status_code == 200 and r.headers.get("Content-Type","").lower().startswith("application/json"):
                out["teams"] = r.json().get("teams", [])
                break
    else:
        out["teams"] = out["scoreboard"].get("teams", [])

    # 3) Boxscore (mMatchup) for lineups/players (best-effort; may not exist in all leagues)
    for host in hosts:
        print(f"[INFO] Fetching boxscore via mMatchup: {host}")
        r = _try_fetch(s, host, {"view": "mMatchup", "scoringPeriodId": str(week)}, cookies)
        if r.status_code == 200 and r.headers.get("Content-Type","").lower().startswith("application/json"):
            out["boxscore"] = r.json()
            break

    return out

# =================
# Transformations
# =================

def _team_display_name(t: dict) -> str:
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

def summarize_matchups(scoreboard, teams, week):
    team_map = {t.get("id"): _team_display_name(t) for t in (teams or []) if t.get("id") is not None}
    print(f"[INFO] Built team name map for {len(team_map)} teams")

    matchups = []
    for m in (scoreboard.get("schedule") or []):
        if m.get("matchupPeriodId") != int(week): 
            continue
        if "away" not in m or "home" not in m:
            continue
        hid = m["home"]["teamId"]
        aid = m["away"]["teamId"]
        home_pts = float(m["home"].get("totalPoints", 0) or 0)
        away_pts = float(m["away"].get("totalPoints", 0) or 0)

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

        matchups.append({
            "home_id": hid,
            "away_id": aid,
            "home": team_map.get(hid, f"Team {hid}"),
            "away": team_map.get(aid, f"Team {aid}"),
            "home_pts": round(home_pts, 2),
            "away_pts": round(away_pts, 2),
            "winner": winner,
            "abs_margin": round(abs(home_pts - away_pts), 2),
        })
    return matchups

def extract_standings(teams):
    def get_record_fields(t: dict):
        rec = (t.get("record") or {}).get("overall") or {}
        wins = rec.get("wins")
        losses = rec.get("losses")
        ties = rec.get("ties")
        points_against = rec.get("pointsAgainst") if isinstance(rec, dict) else None
        # fallbacks
        wins = t.get("overallWins", wins)
        losses = t.get("overallLosses", losses)
        ties = t.get("overallTies", ties)
        return (int(wins or 0), int(losses or 0), int(ties or 0), float(points_against or 0))

    def get_points_for(t: dict) -> float:
        pf = t.get("points")
        if isinstance(pf, (int, float)): return float(pf)
        if isinstance(pf, dict):
            val = pf.get("scored")
            if isinstance(val, (int, float)): return float(val)
        vbs = t.get("valuesByStat") or {}
        stat0 = vbs.get("0")
        if isinstance(stat0, (int, float)): return float(stat0)
        return 0.0

    rows = []
    for t in (teams or []):
        name = _team_display_name(t)
        wins, losses, ties, pa = get_record_fields(t)
        pf = round(get_points_for(t), 2)
        rows.append({"name": name, "wins": wins, "losses": losses, "ties": ties, "points_for": pf, "points_against": round(pa, 2)})
    rows.sort(key=lambda r: (r["wins"], r["points_for"]), reverse=True)
    return rows

# ==========================
# Build week/player details
# ==========================

def _safe_entries(side):
    """
    Returns list of roster entries for a side (home/away) in boxscore schedule.
    ESPN often stores under side['rosterForCurrentScoringPeriod']['entries'].
    """
    if not isinstance(side, dict):
        return []
    r = side.get("rosterForCurrentScoringPeriod") or side.get("rosterForMatchupPeriod")
    entries = (r or {}).get("entries") if isinstance(r, dict) else None
    return entries or []

def build_week_stats_from_boxscore(boxscore, teams, week):
    """
    Produce per-team rows with player-level context for the week.
    Each row: {team, pts, opp, opp_pts, won, abs_margin, starters:[...], bench:[...], proj: float|None}
    Player item: {name, posId, slotId, points, proj}
    """
    if not isinstance(boxscore, dict):
        return None

    team_map = {t.get("id"): _team_display_name(t) for t in (teams or []) if t.get("id") is not None}
    rows = []
    wk = int(week)

    def _extract_points(e, ppe, player, wk):
        # 1) Entry-level totals
        for k in ("appliedStatTotal", "appliedTotal", "points", "totalPoints"):
            v = e.get(k)
            if isinstance(v, (int, float)):
                return float(v)
        # 2) PlayerPoolEntry-level totals
        if isinstance(ppe, dict):
            for k in ("appliedStatTotal", "appliedTotal", "points", "totalPoints"):
                v = ppe.get(k)
                if isinstance(v, (int, float)):
                    return float(v)
        # 3) Player.stats for this scoring period (statSourceId 0 = actuals)
        stats = (player or {}).get("stats")
        if isinstance(stats, list):
            for st in stats:
                try:
                    if int(st.get("scoringPeriodId")) == wk and st.get("statSourceId") in (0,):
                        val = st.get("appliedTotal") or st.get("appliedStatTotal") or st.get("points")
                        if isinstance(val, (int, float)):
                            return float(val)
                except Exception:
                    continue
        # 4) Sum appliedStats as a last resort
        apps = e.get("appliedStats")
        if isinstance(apps, dict):
            total = 0.0
            any_num = False
            for v in apps.values():
                if isinstance(v, (int, float)):
                    total += float(v)
                    any_num = True
            if any_num:
                return total
        return 0.0

    def _extract_proj(e, ppe, wk_key):
        # Try entry.ratings[wk] and ppe.ratings[wk]
        ratings = e.get("ratings")
        if isinstance(ratings, dict):
            rW = ratings.get(wk_key) or ratings.get("0") or {}
            for k in ("totalProjection", "totalProjectedPoints", "totalProjectPoints", "totalProjectionPoints"):
                if isinstance(rW.get(k), (int, float)):
                    return float(rW[k])
        pr = ppe.get("ratings") if isinstance(ppe, dict) else None
        if isinstance(pr, dict):
            rW = pr.get(wk_key) or pr.get("0") or {}
            for k in ("totalProjection", "totalProjectedPoints", "totalProjectPoints", "totalProjectionPoints"):
                if isinstance(rW.get(k), (int, float)):
                    return float(rW[k])
        # Common direct fields
        for k in ("projectedPoints", "projectedTotal", "pointsProjected"):
            v = e.get(k)
            if isinstance(v, (int, float)):
                return float(v)
        return None

    def parse_entries(e_list):
        out = []
        wk_key = str(wk)
        for e in (e_list or []):
            try:
                ppe = e.get("playerPoolEntry", {}) if isinstance(e, dict) else {}
                player = ppe.get("player", {}) if isinstance(ppe, dict) else {}
                full = player.get("fullName") or player.get("name") or "Player"
                posId = player.get("defaultPositionId")
                slotId = e.get("lineupSlotId")

                pts = _extract_points(e, ppe, player, wk)
                proj = _extract_proj(e, ppe, wk_key)

                out.append({
                    "name": full,
                    "posId": posId,
                    "slotId": slotId,
                    "points": round(float(pts), 2),
                    "proj": float(proj) if isinstance(proj, (int, float)) else None,
                })
            except Exception:
                continue
        return out

    def _safe_entries(side):
        if not isinstance(side, dict):
            return []
        r = side.get("rosterForCurrentScoringPeriod") or side.get("rosterForMatchupPeriod")
        return (r or {}).get("entries") or []

    for m in (boxscore.get("schedule") or []):
        if m.get("matchupPeriodId") != wk:
            continue
        home = m.get("home") or {}
        away = m.get("away") or {}
        hid, aid = home.get("teamId"), away.get("teamId")

        h_entries = parse_entries(_safe_entries(home))
        a_entries = parse_entries(_safe_entries(away))

        # Starters = not Bench and not IR
        EXCLUDE = {LINEUP_SLOT_BENCH, LINEUP_SLOT_IR}
        h_starters = [x for x in h_entries if x.get("slotId") not in EXCLUDE]
        h_bench    = [x for x in h_entries if x.get("slotId") in EXCLUDE]
        a_starters = [x for x in a_entries if x.get("slotId") not in EXCLUDE]
        a_bench    = [x for x in a_entries if x.get("slotId") in EXCLUDE]

        hpts = float(home.get("totalPoints", 0) or 0)
        apts = float(away.get("totalPoints", 0) or 0)
        margin = abs(hpts - apts)
        winner = "home" if hpts > apts else ("away" if apts > hpts else "tie")

        def sum_proj(starters):
            projs = [p["proj"] for p in starters if isinstance(p.get("proj"), (int, float))]
            return round(sum(projs), 2) if projs else None

        rows.append({
            "team": team_map.get(hid, f"Team {hid}"),
            "pts": round(hpts, 2),
            "opp": team_map.get(aid, f"Team {aid}"),
            "opp_pts": round(apts, 2),
            "won": winner == "home",
            "abs_margin": round(margin, 2),
            "starters": h_starters,
            "bench": h_bench,
            "proj": sum_proj(h_starters),
        })
        rows.append({
            "team": team_map.get(aid, f"Team {aid}"),
            "pts": round(apts, 2),
            "opp": team_map.get(hid, f"Team {hid}"),
            "opp_pts": round(hpts, 2),
            "won": winner == "away",
            "abs_margin": round(margin, 2),
            "starters": a_starters,
            "bench": a_bench,
            "proj": sum_proj(a_starters),
        })

    return rows

# ======================
# Weekly challenge logic
# ======================

def compute_week_challenge(week:int, matchups, standings, week_rows):
    """
    Returns dict {title:'Weekly Challenge Winner', winner:'Team ...', detail:'...'} or None.
    Implements your updated rotation for Weeks 1–13.
    """

    # --- helpers reused across weeks ---

    def highest_scoring_team():
        all_rows = []
        for m in matchups:
            all_rows.append({"team": m["home"], "pts": m["home_pts"]})
            all_rows.append({"team": m["away"], "pts": m["away_pts"]})
        if not all_rows: return None
        row = max(all_rows, key=lambda r: r["pts"])
        return ("Highest scoring team", row["team"], f"{row['pts']} pts")

    def team_with_highest_scoring_player_starters_incl_dst():
        if not week_rows: return None
        best = None
        for r in week_rows:
            for p in (r.get("starters") or []):
                pts = p.get("points", 0)
                if best is None or pts > best["points"]:
                    best = {"team": r["team"], "player": p.get("name","Player"), "points": pts}
        if not best: return None
        return ("Highest scoring player (starter, D/ST incl.)", best["team"], f"{best['player']} — {best['points']} pts")

    def team_with_highest_scoring_bench_player():
        if not week_rows: return None
        worst = None
        for r in week_rows:
            for p in (r.get("bench") or []):
                pts = p.get("points", 0)
                if worst is None or pts < worst["points"]:
                    worst = {"team": r["team"], "player": p.get("name","Player"), "points": pts}
        if not worst: return None
        return ("Highest scoring bench player", worst["team"], f"{worst['player']} — {worst['points']} pts")

    def smallest_margin_of_victory():
        winners = [m for m in matchups if m["winner"] in ("home","away")]
        if not winners: return None
        m = min(winners, key=lambda x: x["abs_margin"])
        win_team = m["home"] if m["winner"]=="home" else m["away"]
        return ("Smallest margin of victory", win_team, f"margin {m['abs_margin']}")

    def widest_margin_of_victory():
        winners = [m for m in matchups if m["winner"] in ("home","away")]
        if not winners: return None
        m = max(winners, key=lambda x: x["abs_margin"])
        win_team = m["home"] if m["winner"]=="home" else m["away"]
        return ("Widest margin of victory", win_team, f"margin {m['abs_margin']}")

    def highest_scoring_starting_k():
        if not week_rows: return None
        best = None
        for r in week_rows:
            for p in (r.get("starters") or []):
                if p.get("posId") == POS_K:
                    pts = p.get("points", 0)
                    if best is None or pts > best["points"]:
                        best = {"team": r["team"], "player": p.get("name","K"), "points": pts}
        if not best: return None
        return ("Highest scoring starting K", best["team"], f"{best['player']} — {best['points']} pts")

    def highest_scoring_starting_qb():
        if not week_rows: return None
        best = None
        for r in week_rows:
            for p in (r.get("starters") or []):
                if p.get("posId") == POS_QB:
                    pts = p.get("points", 0)
                    if best is None or pts > best["points"]:
                        best = {"team": r["team"], "player": p.get("name","QB"), "points": pts}
        if not best: return None
        return ("Highest scoring starting QB", best["team"], f"{best['player']} — {best['points']} pts")

    def most_points_scored_in_losing_effort():
        rows = []
        for m in matchups:
            if m["winner"] == "home":
                rows.append({"team": m["away"], "pts": m["away_pts"]})
            elif m["winner"] == "away":
                rows.append({"team": m["home"], "pts": m["home_pts"]})
        if not rows: return None
        r = max(rows, key=lambda x: x["pts"])
        return ("Most points in a losing effort", r["team"], f"{r['pts']} pts")

    def first_place_after_week9():
        if week < 9 or not standings: return None
        top = standings[0]
        ties = f"-{top['ties']}" if top.get("ties") else ""
        return ("First place overall (after 9 weeks)", top["name"], f"{top['wins']}-{top['losses']}{ties}, PF {top['points_for']}")

    def team_with_dst_most_points():
        if not week_rows: return None
        best = None
        for r in week_rows:
            for p in (r.get("starters") or []):
                if p.get("posId") == POS_DST:
                    pts = p.get("points", 0)
                    if best is None or pts > best["points"]:
                        best = {"team": r["team"], "player": p.get("name","D/ST"), "points": pts}
        if not best: return None
        return ("D/ST with most points", best["team"], f"{best['player']} — {best['points']} pts")

    def highest_combined_starting_rb_points_incl_flex():
        if not week_rows: return None
        best_team = None
        best_sum = -1.0
        for r in week_rows:
            total = 0.0
            for p in (r.get("starters") or []):
                if p.get("posId") == POS_RB:
                    total += float(p.get("points",0) or 0)
            if total > best_sum:
                best_sum = total
                best_team = r["team"]
        if best_team is None: return None
        return ("Highest combined starting RB points", best_team, f"{round(best_sum,2)} pts")

    def team_closest_to_projected_total():
        if not week_rows: return None
        best = None
        for r in week_rows:
            proj_sum = 0.0
            have_proj = False
            for p in (r.get("starters") or []):
                pr = None
                for k in ("projectedPoints", "projectedTotal", "proj", "pointsProjected"):
                    pr = pr or p.get(k)
                if isinstance(pr, (int, float)):
                    proj_sum += float(pr)
                    have_proj = True
            if not have_proj:
                continue
            diff = abs(r["pts"] - proj_sum)
            if best is None or diff < best["diff"]:
                best = {"team": r["team"], "proj": round(proj_sum,2), "actual": r["pts"], "diff": round(diff,2)}
        if not best:
            return None
        return ("Closest to projected total", best["team"], f"diff {best['diff']} (proj {best['proj']} vs {best['actual']})")

    def most_points_against_cumulative():
        if not standings: return None
        r = max(standings, key=lambda t: t.get("points_against", 0))
        pa = r.get("points_against", 0)
        return ("Most points against (season)", r["name"], f"{pa} against")

    def lowest_scoring_team():
        all_rows = []
        for m in matchups:
            all_rows.append({"team": m["home"], "pts": m["home_pts"]})
            all_rows.append({"team": m["away"], "pts": m["away_pts"]})
        if not all_rows: return None
        row = min(all_rows, key=lambda r: r["pts"])
        return ("Lowest weekly score", row["team"], f"{row['pts']} pts")

    def highest_scoring_flex():
        if not week_rows: return None
        best = None
        for r in week_rows:
            for p in (r.get("starters") or []):
                if p.get("slotId") == LINEUP_SLOT_FLEX:
                    pts = p.get("points", 0)
                    if best is None or pts > best["points"]:
                        best = {"team": r["team"], "player": p.get("name","FLEX"), "points": pts}
        if not best: return None
        return ("Highest scoring FLEX", best["team"], f"{best['player']} — {best['points']} pts")

    def highest_scoring_te():
        if not week_rows: return None
        best = None
        for r in week_rows:
            for p in (r.get("starters") or []):
                if p.get("posId") == POS_TE:
                    pts = p.get("points", 0)
                    if best is None or pts > best["points"]:
                        best = {"team": r["team"], "player": p.get("name","TE"), "points": pts}
        if not best: return None
        return ("Highest scoring TE", best["team"], f"{best['player']} — {best['points']} pts")

    def first_team_out_overall():
        # 7th-place team in current standings (list already sorted in extract_standings)
        if not standings or len(standings) < 7:
            return None
        t = standings[6]
        rec = f"{t['wins']}-{t['losses']}" + (f"-{t['ties']}" if t.get("ties") else "")
        return ("First Team Out", t["name"], f"7th place • {rec}, PF {t['points_for']}")

    mapping = {
        1:  highest_scoring_team,
        2:  team_with_highest_scoring_player_starters_incl_dst,
        3:  team_with_highest_scoring_bench_player,
        4:  smallest_margin_of_victory,
        5:  widest_margin_of_victory,
        6:  highest_scoring_starting_k,
        7:  highest_scoring_starting_qb,
        8:  most_points_scored_in_losing_effort,
        9:  first_place_after_week9,
        10: team_with_dst_most_points,
        11: highest_combined_starting_rb_points_incl_flex,
        12: team_closest_to_projected_total,
        13: most_points_against_cumulative,
        14: lowest_scoring_team,          # NEW
        15: highest_scoring_flex,         # NEW
        16: highest_scoring_te,           # NEW
        17: first_team_out_overall,       # NEW (Week 17 = 7th place)
    }

    fn = mapping.get(int(week))
    if not fn:
        return None
    try:
        res = fn() if callable(fn) else None
        if not res:
            return None
        title, winner, detail = res
        return {"title": "Weekly Challenge Winner", "winner": winner, "detail": detail, "subtitle": title}
    except Exception as e:
        print(f"[WARN] Challenge computation failed: {e}", file=sys.stderr)
        return None

# ============================
# Upcoming challenge (new)
# ============================

def get_challenge_title_by_week(week:int) -> str | None:
    titles = {
        1:  "Highest scoring team",
        2:  "Highest scoring player (starter, D/ST incl.)",
        3:  "Highest scoring bench player",
        4:  "Smallest margin of victory",
        5:  "Widest margin of victory",
        6:  "Highest scoring starting K",
        7:  "Highest scoring starting QB",
        8:  "Most points in a losing effort",
        9:  "First place overall (after 9 weeks)",
        10: "D/ST with most points",
        11: "Highest combined starting RB points",
        12: "Closest to projected total",
        13: "Most points against (season)",
        14: "Lowest weekly score",          # NEW
        15: "Highest scoring FLEX",         # NEW
        16: "Highest scoring TE",           # NEW
        17: "First Team Out",               # NEW
    }
    return titles.get(int(week))

def describe_upcoming_challenge(current_week:int) -> dict | None:
    next_week = int(current_week) + 1
    title = get_challenge_title_by_week(next_week)
    if not title:
        return None
    return {
        "label": f"Next Week's Challenge (Week {next_week})",
        "subtitle": title
    }

# ============================
# Power rankings (new)
# ============================

def compute_power_rankings(standings: list[dict]) -> list[dict]:
    """
    Compute a simple weekly power ranking:
      score = 2*wins - losses + (points_for - points_against) / 100
    Sort by score desc, then PF desc.
    """
    if not standings:
        return []
    rows = []
    for r in standings:
        wins = r.get("wins", 0)
        losses = r.get("losses", 0)
        pf = float(r.get("points_for", 0))
        pa = float(r.get("points_against", 0))
        score = (2 * wins) - losses + (pf - pa) / 100.0
        rows.append({
            "name": r["name"],
            "record": f"{wins}-{losses}" + (f"-{r['ties']}" if r.get("ties") else ""),
            "pf": round(pf, 2),
            "pa": round(pa, 2),
            "score": round(score, 3)
        })
    rows.sort(key=lambda x: (x["score"], x["pf"]), reverse=True)
    for i, r in enumerate(rows, start=1):
        r["rank"] = i
    return rows

# =========================================
# NEW: Build full Weekly Challenges section
# =========================================

MAX_CHALLENGE_WEEK = 17  # rotation covers weeks 1–17

def _fetch_week_bits(season:int, week:int):
    """Fetch scoreboard/teams/box + transform for a given week."""
    payloads = espn_fetch_jsons(LEAGUE_ID, season, week)
    scoreboard = payloads["scoreboard"]
    teams = payloads["teams"]
    boxscore = payloads["boxscore"]
    matchups = summarize_matchups(scoreboard, teams, week)
    week_rows = build_week_stats_from_boxscore(boxscore, teams, week)
    standings = extract_standings(teams)
    return matchups, standings, week_rows

def build_weekly_challenges(season:int, current_week:int) -> list[dict]:
    """
    Returns rows: [{week, title, winner, detail}]
    - For weeks <= current_week: compute winner from that week’s data.
    - For weeks  > current_week: 'TBD' winner with challenge title.
    """
    rows = []
    for wk in range(1, MAX_CHALLENGE_WEEK + 1):
        title = get_challenge_title_by_week(wk) or f"Challenge Week {wk}"
        if wk <= current_week:
            try:
                matchups, standings, week_rows = _fetch_week_bits(season, wk)
                ch = compute_week_challenge(wk, matchups, standings, week_rows)
                if ch:
                    rows.append({
                        "week": wk,
                        "title": title,
                        "winner": ch["winner"],
                        "detail": ch["detail"]
                    })
                else:
                    rows.append({"week": wk, "title": title, "winner": "—", "detail": ""})
            except Exception as e:
                print(f"[WARN] Weekly challenge W{wk} failed: {e}", file=sys.stderr)
                rows.append({"week": wk, "title": title, "winner": "—", "detail": ""})
        else:
            rows.append({"week": wk, "title": title, "winner": "TBD", "detail": ""})
    return rows

# ===============
# Narrative blurb
# ===============

def build_narrative(matchups, week, week_rows=None):
    """
    Week recap with:
      - closest game
      - biggest blowout
      - rotating tease for any team >20 below projection (if projections available)
      - rotating jab for the week's lowest score
    Rotation index = (week-1) % 3
    """
    if not matchups:
        return f"No results yet for Week {week}."

    closest = min(matchups, key=lambda m: m["abs_margin"])
    blowout = max(matchups, key=lambda m: m["abs_margin"])
    lowest_pair = min(matchups, key=lambda m: min(m["home_pts"], m["away_pts"]))
    loser_team = lowest_pair["home"] if lowest_pair["home_pts"] < lowest_pair["away_pts"] else lowest_pair["away"]
    loser_score = min(lowest_pair["home_pts"], lowest_pair["away_pts"])

    lines = []
    lines.append(f"Week {week} is in the books!")
    lines.append(
        f" The closest battle was between {closest['away']} and {closest['home']}, "
        f"decided by just {closest['abs_margin']} points."
    )
    lines.append(
        f" Meanwhile, {blowout['home']} vs {blowout['away']} was a blowout "
        f"with a margin of {blowout['abs_margin']}."
    )

    idx = (int(week) - 1) % 3
    if isinstance(week_rows, list) and week_rows:
        underperf = []
        for r in week_rows:
            proj = r.get("proj")
            pts = r.get("pts", 0)
            if isinstance(proj, (int, float)) and (proj - pts) >= 20:
                underperf.append({"team": r["team"], "proj": float(proj), "pts": float(pts), "delta": round(float(proj - pts), 2)})
        if underperf:
            worst = max(underperf, key=lambda x: x["delta"])
            u_msgs = [
                f"{worst['team']} missed the memo: projected {worst['proj']:.1f}, delivered {worst['pts']:.1f} (−{worst['delta']:.1f}).",
                f"{worst['team']} got humbled—{worst['proj']:.1f} projected, only {worst['pts']:.1f}. That’s a {worst['delta']:.1f}-point faceplant.",
                f"Vegas had {worst['team']} at {worst['proj']:.1f}; reality said {worst['pts']:.1f}. {worst['delta']:.1f} under. Yikes.",
            ]
            lines.append(" " + u_msgs[idx])

    l_msgs = [
        f"And bringing up the rear, {loser_team} with just {loser_score:.1f}. Someone check their Wi-Fi.",
        f"Weekly floor goes to {loser_team}: {loser_score:.1f} points. Bench might sue for playing time.",
        f"{loser_team} posted {loser_score:.1f}. The kicker’s carpool scored more.",
    ]
    lines.append(" " + l_msgs[idx])

    return " ".join(lines)

# =============
# HTML template
# =============

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
  <td style="background:#0f172a; color:#ffffff; padding:18px 28px; font-family:Arial, Helvetica, sans-serif;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="width:100%; min-width:100%;">
      <tr>
        <!-- Left: Title -->
        <td align="left" valign="middle" style="padding-right:12px;">
          <div style="font-size:22px; line-height:1.2; font-weight:700; letter-spacing:.3px; color:#ffffff;">
            Geno's Weekly
          </div>
        </td>

        <!-- Right: Logo (fixed width so it doesn't collapse on mobile) -->
        <td align="right" valign="middle" width="120" style="width:120px;">
          {% if logo_url %}
          <img src="{{ logo_url }}"
               alt="Geno's Weekly"
               width="120"  <!-- attribute matters on mobile clients -->
               style="display:block; width:120px; height:auto; border:0; outline:none; text-decoration:none;">
          {% endif %}
        </td>
      </tr>
    </table>
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

            <!-- Weekly Challenge Winner -->
            {% if challenge %}
            <tr>
              <td style="padding:0 24px 8px 24px; font-family:Arial, Helvetica, sans-serif;">
                <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background:#ecfdf5; border:1px solid #10b981; border-radius:10px;">
                  <tr>
                    <td style="padding:14px 16px;">
                      <div style="font-size:15px; font-weight:700; color:#065f46; margin-bottom:4px;">
                        🏅 Weekly Challenge Winner — {{ challenge.subtitle }}
                      </div>
                      <div style="font-size:14px; color:#065f46;">
                        <strong>{{ challenge.winner }}</strong> <span style="opacity:.85;">({{ challenge.detail }})</span>
                      </div>
                    </td>
                  </tr>
                </table>
              </td>
            </tr>
            {% endif %}

            <!-- Upcoming Weekly Challenge -->
            {% if next_challenge %}
            <tr>
              <td style="padding:0 24px 8px 24px; font-family:Arial, Helvetica, sans-serif;">
                <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background:#eff6ff; border:1px solid #3b82f6; border-radius:10px;">
                  <tr>
                    <td style="padding:14px 16px;">
                      <div style="font-size:15px; font-weight:700; color:#1e40af; margin-bottom:4px;">
                        🔮 {{ next_challenge.label }}
                      </div>
                      <div style="font-size:14px; color:#1e3a8a;">
                        {{ next_challenge.subtitle }}
                      </div>
                    </td>
                  </tr>
                </table>
              </td>
            </tr>
            {% endif %}

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
                              <strong style="color:#065f46;">Winner: {{ m.home }}</strong> (margin {{ m.abs_margin }})
                            {% elif m.winner=='away' %}
                              <strong style="color:#065f46;">Winner: {{ m.away }}</strong> (margin {{ m.abs_margin }})
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

            <!-- Standings -->
            {% if standings and standings|length > 0 %}
            <tr>
              <td style="padding:4px 24px 12px 24px; font-family:Arial, Helvetica, sans-serif;">
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

            <!-- Power Rankings -->
            {% if power and power|length > 0 %}
            <tr>
              <td style="padding:4px 24px 20px 24px; font-family:Arial, Helvetica, sans-serif;">
                <div style="font-size:16px; font-weight:700; color:#0f172a; margin:14px 0 8px;">Power Rankings</div>
                <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse; border:1px solid #e5e7eb;">
                  <thead>
                    <tr style="background:#f1f5f9;">
                      <th align="left" style="padding:8px 10px; font-size:12px; color:#334155; border-bottom:1px solid #e5e7eb;">#</th>
                      <th align="left" style="padding:8px 10px; font-size:12px; color:#334155; border-bottom:1px solid #e5e7eb;">Team</th>
                      <th align="center" style="padding:8px 10px; font-size:12px; color:#334155; border-bottom:1px solid #e5e7eb;">Record</th>
                      <th align="right" style="padding:8px 10px; font-size:12px; color:#334155; border-bottom:1px solid #e5e7eb;">PF</th>
                      <th align="right" style="padding:8px 10px; font-size:12px; color:#334155; border-bottom:1px solid #e5e7eb;">PA</th>
                      <th align="right" style="padding:8px 10px; font-size:12px; color:#334155; border-bottom:1px solid #e5e7eb;">Score</th>
                    </tr>
                  </thead>
                  <tbody>
                    {% for r in power %}
                    <tr>
                      <td style="padding:8px 10px; font-size:13px; color:#0f172a; border-bottom:1px solid #e5e7eb;">{{ r.rank }}</td>
                      <td style="padding:8px 10px; font-size:13px; color:#0f172a; border-bottom:1px solid #e5e7eb;">{{ r.name }}</td>
                      <td align="center" style="padding:8px 10px; font-size:13px; color:#334155; border-bottom:1px solid #e5e7eb;">{{ r.record }}</td>
                      <td align="right" style="padding:8px 10px; font-size:13px; color:#334155; border-bottom:1px solid #e5e7eb;">{{ r.pf }}</td>
                      <td align="right" style="padding:8px 10px; font-size:13px; color:#334155; border-bottom:1px solid #e5e7eb;">{{ r.pa }}</td>
                      <td align="right" style="padding:8px 10px; font-size:13px; color:#334155; border-bottom:1px solid #e5e7eb;">{{ r.score }}</td>
                    </tr>
                    {% endfor %}
                  </tbody>
                </table>
                <div style="font-size:11px; color:#94a3b8; margin-top:6px;">Score = 2×Wins − Losses + (PF − PA)/100</div>
              </td>
            </tr>
            {% endif %}

   <!-- Weekly Challenges (ALL WEEKS) -->
            {% if weekly_challenges and weekly_challenges|length > 0 %}
            <tr>
              <td style="padding:12px 24px 6px 24px; font-family:Arial, Helvetica, sans-serif;">
                <div style="font-size:16px; font-weight:700; color:#0f172a; margin-bottom:10px;">Weekly Challenges</div>
                <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse; border:1px solid #e5e7eb;">
                  <thead>
                    <tr style="background:#f1f5f9;">
                      <th align="left" style="padding:8px 10px; font-size:12px; color:#334155; border-bottom:1px solid #e5e7eb;">Week</th>
                      <th align="left" style="padding:8px 10px; font-size:12px; color:#334155; border-bottom:1px solid #e5e7eb;">Challenge</th>
                      <th align="left" style="padding:8px 10px; font-size:12px; color:#334155; border-bottom:1px solid #e5e7eb;">Winner</th>
                      <th align="left" style="padding:8px 10px; font-size:12px; color:#334155; border-bottom:1px solid #e5e7eb;">Details</th>
                    </tr>
                  </thead>
                  <tbody>
                    {% for row in weekly_challenges %}
                    <tr>
                      <td style="padding:8px 10px; font-size:13px; color:#0f172a; border-bottom:1px solid #e5e7eb;">W{{ row.week }}</td>
                      <td style="padding:8px 10px; font-size:13px; color:#0f172a; border-bottom:1px solid #e5e7eb;">{{ row.title }}</td>
                      <td style="padding:8px 10px; font-size:13px; color:#0f172a; border-bottom:1px solid #e5e7eb;">{{ row.winner }}</td>
                      <td style="padding:8px 10px; font-size:13px; color:#334155; border-bottom:1px solid #e5e7eb;">{{ row.detail }}</td>
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
                Sent from the Geno's league headquarters by Commissioner Lally's office • Week {{ week }}<br>
                Generated {{ now }}
              </td>
            </tr>
            
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>
""".strip())

# =====
# Main
# =====

def main():
    today = datetime.date.today()
    season = int(SEASON) if SEASON else today.year
    week = int(WEEK) if WEEK else 1

    # current week payload
    payloads = espn_fetch_jsons(LEAGUE_ID, season, week)
    scoreboard = payloads["scoreboard"]
    teams = payloads["teams"]
    boxscore = payloads["boxscore"]  # may be None if league hides it

    matchups = summarize_matchups(scoreboard, teams, week)
    standings = extract_standings(teams)
    week_rows = build_week_stats_from_boxscore(boxscore, teams, week)

    # compute Weekly Challenge for current week
    challenge = compute_week_challenge(week, matchups, standings, week_rows)

    # NEW: compute Power Rankings & Upcoming Challenge
    power = compute_power_rankings(standings)
    next_challenge = describe_upcoming_challenge(week)

    # NEW: build full Weekly Challenges table (Weeks 1–13)
    weekly_challenges = build_weekly_challenges(season, week)

    narrative = build_narrative(matchups, week, week_rows)

    if not matchups:
        print("[WARN] No matchups found for that week/season.", file=sys.stderr)

    html = HTML_TMPL.render(
        week=week,
        matchups=matchups,
        standings=standings,
        narrative=narrative,
        challenge=challenge,
        next_challenge=next_challenge,
        power=power,
        weekly_challenges=weekly_challenges,
        logo_url=LOGO_URL,            # <— LOGO
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
