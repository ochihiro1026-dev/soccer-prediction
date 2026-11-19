import json
import math
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))

# 対象リーグ（The Odds APIのsportキー：無料枠500クレジット/月を節約するため主要6大会に限定）
TARGET_LEAGUES = [
    ("soccer_epl", "プレミアリーグ"),
    ("soccer_spain_la_liga", "ラ・リーガ"),
    ("soccer_italy_serie_a", "セリエA"),
    ("soccer_germany_bundesliga", "ブンデスリーガ"),
    ("soccer_france_ligue_one", "リーグ・アン"),
    ("soccer_uefa_champs_league", "CL"),
]

# 日本式ハンデ段階リスト（軽い -> 重い）
HANDICAPS = [
    "0", "0/3", "0/5", "0/7", "0/8", "0半",
    "0半3", "0半5", "0半7", "1.0", "1.2", "1.3",
    "1.5", "1.7", "1半", "1半7", "2.0", "2.4", "3.0", "3半4",
]

PAYOUT_RATE = 1.95  # 想定配当倍率（全勝ち時）


def eval_outcome(h_id: str, diff: int):
    if h_id == "0":
        return ("fullWin", 1.0) if diff >= 1 else ("push", 0.0) if diff == 0 else ("fullLose", -1.0)
    if h_id.startswith("0/"):
        k = int(h_id.split("/")[1]) / 10.0
        return ("fullWin", 1.0) if diff >= 1 else ("partLose", -k) if diff == 0 else ("fullLose", -1.0)
    if h_id == "0半":
        return ("fullWin", 1.0) if diff >= 1 else ("fullLose", -1.0)
    if h_id.startswith("0半"):
        k = (10 - int(h_id.replace("0半", ""))) / 10.0
        return ("fullWin", 1.0) if diff >= 2 else ("partWin", k) if diff == 1 else ("fullLose", -1.0)
    if h_id in ("1.0", "1.2", "1.3", "1.5", "1.7"):
        k = int(h_id.split(".")[1]) / 10.0
        if diff >= 2:
            return ("fullWin", 1.0)
        if diff == 1:
            return ("push", 0.0) if k == 0 else ("partLose", -k)
        return ("fullLose", -1.0)
    if h_id == "1半":
        return ("fullWin", 1.0) if diff >= 2 else ("fullLose", -1.0)
    if h_id == "1半7":
        return ("fullWin", 1.0) if diff >= 3 else ("partWin", 0.3) if diff == 2 else ("fullLose", -1.0)
    if h_id in ("2.0", "2.4"):
        k = int(h_id.split(".")[1]) / 10.0
        if diff >= 3:
            return ("fullWin", 1.0)
        if diff == 2:
            return ("push", 0.0) if k == 0 else ("partLose", -k)
        return ("fullLose", -1.0)
    if h_id == "3.0":
        return ("fullWin", 1.0) if diff >= 4 else ("push", 0.0) if diff == 3 else ("fullLose", -1.0)
    if h_id == "3半4":
        return ("fullWin", 1.0) if diff >= 5 else ("partWin", 0.6) if diff == 4 else ("fullLose", -1.0)
    return ("fullLose", -1.0)


def poisson_pmf(k: int, lam: float) -> float:
    return (lam ** k) * math.exp(-lam) / math.factorial(k)


def estimate_goal_diff_probs(p_win: float, p_draw: float, p_lose: float) -> dict:
    total_goals = 2.65
    best_err = 999.0
    best_lh, best_la = 1.4, 1.2

    for i in range(40):
        lh = 0.4 + i * 0.08
        la = max(0.25, total_goals - lh * 0.78)
        w = d = l = 0.0
        for h in range(8):
            ph = poisson_pmf(h, lh)
            for a in range(8):
                p = ph * poisson_pmf(a, la)
                if h > a:
                    w += p
                elif h == a:
                    d += p
                else:
                    l += p
        err = (w - p_win) ** 2 + (d - p_draw) ** 2 + (l - p_lose) ** 2
        if err < best_err:
            best_err = err
            best_lh, best_la = lh, la

    raw_d = {1: 0.0, 2: 0.0, 3: 0.0, 4: 0.0, 5: 0.0}
    for h in range(9):
        ph = poisson_pmf(h, best_lh)
        for a in range(9):
            if h > a:
                diff = min(5, h - a)
                raw_d[diff] += ph * poisson_pmf(a, best_la)

    sum_win = sum(raw_d.values())
    scale = (p_win / sum_win) if sum_win > 0 else 0.0
    return {
        "lose": p_lose,
        0: p_draw,
        1: raw_d[1] * scale,
        2: raw_d[2] * scale,
        3: raw_d[3] * scale,
        4: raw_d[4] * scale,
        5: raw_d[5] * scale,
    }


def calc_metrics(h_id: str, diff_probs: dict, payout_rate: float = PAYOUT_RATE) -> dict:
    diffs = [(-1, diff_probs["lose"]), (0, diff_probs[0]), (1, diff_probs[1]),
             (2, diff_probs[2]), (3, diff_probs[3]), (4, diff_probs[4]), (5, diff_probs[5])]
    full_win = get_prob = ev = 0.0
    profit_unit = payout_rate - 1.0
    for d, p in diffs:
        outcome, ratio = eval_outcome(h_id, d)
        if outcome == "fullWin":
            full_win += p
            get_prob += p
            ev += p * (ratio * profit_unit)
        elif outcome == "partWin":
            get_prob += p
            ev += p * (ratio * profit_unit)
        elif outcome in ("partLose", "fullLose"):
            ev += p * ratio
    return {
        "full_win_pct": round(full_win * 100, 1),
        "get_pct": round(get_prob * 100, 1),
        "ev_pct": round(ev * 100, 1),
    }


def fetch_league_odds(api_key: str, sport_key: str, from_iso: str, to_iso: str) -> list:
    params = urllib.parse.urlencode({
        "apiKey": api_key,
        "regions": "uk,eu",
        "markets": "h2h",
        "oddsFormat": "decimal",
        "commenceTimeFrom": from_iso,
        "commenceTimeTo": to_iso,
    })
    url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": "SoccerPredictorBot/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"[WARN] {sport_key} HTTP {e.code}: {e.read().decode('utf-8', errors='ignore')}")
        return []
    except Exception as e:
        print(f"[WARN] {sport_key} error: {e}")
        return []


def analyze_event(ev: dict, league_name: str):
    home = ev.get("home_team", "")
    away = ev.get("away_team", "")
    bookmakers = ev.get("bookmakers", [])
    if not bookmakers:
        return None

    home_odds_list, draw_odds_list, away_odds_list = [], [], []
    for bm in bookmakers:
        for m in bm.get("markets", []):
            if m.get("key") != "h2h":
                continue
            outcomes = {o["name"]: o["price"] for o in m.get("outcomes", [])}
            if home in outcomes and away in outcomes and "Draw" in outcomes:
                home_odds_list.append(outcomes[home])
                draw_odds_list.append(outcomes["Draw"])
                away_odds_list.append(outcomes[away])

    if not home_odds_list:
        return None

    avg_h = sum(home_odds_list) / len(home_odds_list)
    avg_d = sum(draw_odds_list) / len(draw_odds_list)
    avg_a = sum(away_odds_list) / len(away_odds_list)

    # 本命側（オッズが低い側）を「出し」として判定
    if avg_h <= avg_a:
        fav_side, fav_team, dog_team = "HOME", home, away
        o_fav, o_draw, o_dog = avg_h, avg_d, avg_a
        fav_range = f"{min(home_odds_list):.2f}〜{max(home_odds_list):.2f}"
    else:
        fav_side, fav_team, dog_team = "AWAY", away, home
        o_fav, o_draw, o_dog = avg_a, avg_d, avg_h
        fav_range = f"{min(away_odds_list):.2f}〜{max(away_odds_list):.2f}"

    raw_f, raw_d, raw_l = 1.0 / o_fav, 1.0 / o_draw, 1.0 / o_dog
    total_raw = raw_f + raw_d + raw_l
    p_fav, p_draw, p_dog = raw_f / total_raw, raw_d / total_raw, raw_l / total_raw

    diff_probs = estimate_goal_diff_probs(p_fav, p_draw, p_dog)

    # 推定ハンデ（EVが0%に最も近いハンデ）
    best_h = HANDICAPS[0]
    best_metrics = None
    min_abs_ev = 9999.0
    for h_id in HANDICAPS:
        m = calc_metrics(h_id, diff_probs)
        if abs(m["ev_pct"]) < min_abs_ev:
            min_abs_ev = abs(m["ev_pct"])
            best_h = h_id
            best_metrics = m

    win_pct = round(p_fav * 100, 1)
    # 自動暫定★評価（直前の定性確認前）
    if win_pct >= 62 and best_metrics["get_pct"] >= 60:
        stars = 4
        rec_type = "確信型候補"
    elif best_metrics["get_pct"] >= 58 and best_metrics["ev_pct"] >= -5.0:
        stars = 3
        rec_type = "的中率型候補"
    else:
        stars = 2
        rec_type = "参考（ハンデ確認要）"

    kickoff_jst = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00")).astimezone(JST)

    return {
        "id": ev.get("id"),
        "league": league_name,
        "kickoff_jst": kickoff_jst.strftime("%Y-%m-%d %H:%M"),
        "home": home,
        "away": away,
        "favorite_side": fav_side,
        "favorite_team": fav_team,
        "odds_avg": {"fav": round(o_fav, 2), "draw": round(o_draw, 2), "dog": round(o_dog, 2)},
        "fav_odds_range": fav_range,
        "fair_prob_pct": {"fav": win_pct, "draw": round(p_draw * 100, 1), "dog": round(p_dog * 100, 1)},
        "estimated_handicap": best_h,
        "metrics_at_est": best_metrics,
        "preliminary_type": rec_type,
        "preliminary_stars": stars,
        "bookmaker_count": len(home_odds_list),
    }


def main():
    api_key = os.environ.get("ODDS_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("環境変数 ODDS_API_KEY が設定されていません。")

    now_jst = datetime.now(JST)
    # 当日19:00 JST 〜 翌日09:00 JST
    start_jst = now_jst.replace(hour=19, minute=0, second=0, microsecond=0)
    if now_jst.hour >= 21:
        # 深夜帯に手動実行された場合は当日枠を維持
        start_jst = now_jst.replace(hour=19, minute=0, second=0, microsecond=0)
    end_jst = (start_jst + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)

    from_iso = start_jst.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    to_iso = end_jst.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    results = []
    for sport_key, league_name in TARGET_LEAGUES:
        events = fetch_league_odds(api_key, sport_key, from_iso, to_iso)
        for ev in events:
            parsed = analyze_event(ev, league_name)
            if parsed:
                results.append(parsed)

    results.sort(key=lambda x: (-x["preliminary_stars"], x["kickoff_jst"]))

    os.makedirs("data", exist_ok=True)
    payload = {
        "generated_at_jst": now_jst.strftime("%Y-%m-%d %H:%M:%S"),
        "window_jst": f"{start_jst.strftime('%Y-%m-%d %H:%M')} 〜 {end_jst.strftime('%Y-%m-%d %H:%M')}",
        "match_count": len(results),
        "matches": results,
    }
    with open("data/predictions.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"Updated data/predictions.json ({len(results)} matches)")


if __name__ == "__main__":
    main()