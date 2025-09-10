import os, requests, datetime, sys
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

def espn_get_scoreboard(league_id, season, week):
    cookies = {"espn_s2": ESPN_S2, "SWID": SWID}
    url = f"https://fantasy.espn.com/apis/v3/games/ffl/seasons/{season}/segments/0/leagues/{league_id}?view=mMatchupScore&scoringPeriodId={week}"
    print(f"[INFO] GET {url}")
    r = requests.get(url, cookies=cookies, timeout=30)
    if not r.ok:
        print(f"[ERROR] ESPN HTTP {r.status_code}: {r.text[:500]}", file=sys.stderr)
        r.raise_for_status()
    return r.json()

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
        print("[WARN] No matchups found for that week/season. Check WEEK/SEASON or cookies.", file=sys.stderr)

    html = HTML_TMPL.render(week=week, matchups=matchups, now=datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
    subject = SUBJECT_TMPL.render(week=week)

    os.makedirs("out", exist_ok=True)
    with open("out/body.html", "w", encoding="utf-8") as f: f.write(html)
    with open("out/subject.txt", "w", encoding="utf-8") as f: f.write(subject)
    print("[INFO] Wrote out/body.html and out/subject.txt")

if __name__ == "__main__":
    main()
