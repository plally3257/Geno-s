import os, requests, datetime, sys, time
from jinja2 import Template

def fail(msg):
    print(f"[ERROR] {msg}", file=sys.stderr)
    sys.exit(1)

for req in ["LEAGUE_ID","ESPN_S2","SWID"]:
    if not os.environ.get(req):
        fail(f"Missing env var {req}. Add it under GitHub Secrets.")

LEAGUE_ID = os.environ["LEAGUE_ID"]
SEASON = os.environ.get("SEASON")
WEEK = os.environ.get("WEEK")
ESPN_S2 = os.environ["ESPN_S2"]
SWID = os.environ["SWID"]

HEADERS = {
    # Make this look like a normal Chrome request
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://fantasy.espn.com",
    "Referer": f"https://fantasy.espn.com/football/league?leagueId={LEAGUE_ID}",
    "Connection": "keep-alive",
}

def espn_get_scoreboard(league_id, season, week):
    cookies = {"espn_s2": ESPN_S2, "SWID": SWID}
    url = f"https://fantasy.espn.com/apis/v3/games/ffl/seasons/{season}/segments/0/leagues/{league_id}"
    params = {"view": "mMatchupScore", "scoringPeriodId": str(week)}
    print(f"[INFO] GET {url}  params={params}")

    s = requests.Session()
    s.headers.update(HEADERS)

    # retry a few times in case CDN blocks one attempt
    for i in range(3):
        r = s.get(url, params=params, cookies=cookies, timeout=30)
        print(f"[INFO] Attempt {i+1}: HTTP {r.status_code}")
        if r.status_code == 200:
            return r.json()
        if r.status_code in (403, 429):
            time.sleep(2 + i)  # small backoff and try again
            continue
        # Other codes -> raise
        print(f"[ERROR] ESPN HTTP {r.status_code}: {r.text[:400]}", file=sys.stderr)
        r.raise_for_status()

    # If we’re here, we kept getting 403/429
    print(f"[ERROR] ESPN blocking the request (HTTP {r.status_code}). Check cookies and SWID braces, then try again.", file=sys.stderr)
    r.raise_for_status()

def summarize_matchups(data, week):
    teams = {t["id"]: t["location"] + " " + t["nickname"] for t in data["teams"]}
    out = []
    for m in data["schedule"]:
        if m.get("matchupPeriodId") != int(week): continue
        if "away" not in m or "home" not in m: continue
        home = teams.get(m["home"]["teamId"], f"Team {m['home']['teamId']}")
        away = teams.get(m["away"]["teamId"], f"Team {m['away']['teamId']}")
        hs = m["home"].get("totalPoints", 0)
        as_ = m["away"].get("totalPoints", 0)
        status = m.get("winner", "UNDECIDED")
        out.append({"home":home, "away":away, "home_pts":hs, "away_pts":as_, "status":status})
    return out

HTML_TMPL = Template("""
<h2>Week {{ week }} Results</h2>
<table border="0" cellpadding="6" cellspacing="0">
  <thead><tr><th align="left">Away</th><th>Score</th><th align="left">Home</th><th>Status</th></tr></thead>
  <tbody>
  {% for m in matchups %}
    <tr>
      <td>{{ m.away }}</td><td align="center">{{ m.away_pts }} - {{ m.home_pts }}</td>
      <td>{{ m.home }}</td><td>{{ m.status }}</td>
    </tr>
  {% endfor %}
  </tbody>
</table>
<p style="font-size:12px;color:#666;">Generated {{ now }}</p>
""".strip())

SUBJECT_TMPL = Template("Fantasy Week {{ week }} Results & Notes")

def main():
    today = datetime.date.today()
    season = int(SEASON) if SEASON else today.year
    week = int(WEEK) if WEEK else 1

    data = espn_get_scoreboard(LEAGUE_ID, season, week)
    matchups = summarize_matchups(data, week)

    if not matchups:
        print("[WARN] No matchups found for that week/season.", file=sys.stderr)

    os.makedirs("out", exist_ok=True)
    with open("out/body.html", "w", encoding="utf-8") as f:
        f.write(HTML_TMPL.render(week=week, matchups=matchups, now=datetime.datetime.now().strftime("%Y-%m-%d %H:%M")))
    with open("out/subject.txt", "w", encoding="utf-8") as f:
        f.write(SUBJECT_TMPL.render(week=week))
    print("[INFO] Wrote out/body.html and out/subject.txt")

if __name__ == "__main__":
    main()
