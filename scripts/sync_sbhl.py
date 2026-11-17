import os
import json
import time
import base64
import requests
from pathlib import Path
from collections import defaultdict
from yfpy.query import YahooFantasySportsQuery

SEED_LEAGUE_ID = "ew_refresh,"

# Yahoo NHL game_ids from 2018-19 through 2027-28 to auto-locate your seed league
CANDIDATE_NHL_GAME_IDS = [475, 465, 453, 427, 419, 411, 403, 396, 386, 380]

MANAGER_NAME_MAP = {
    # "YAHOO_GUID_HERE": "Donovan",
}

def unwrap(obj):
    return obj[0] if isinstance(obj, list) and len(obj) == 1 else obj

def bootstrap_headless_token(auth_dir: Path):
    """Exchanges the permanent Yahoo refresh token for a fresh access token in CI/CD."""
    client_id = os.environ.get("YAHOO_CLIENT_ID", "").strip().strip('"').strip("'")
    client_secret = os.environ.get("YAHOO_CLIENT_SECRET", "").strip().strip('"').strip("'")
    refresh_token = os.environ.get("YAHOO_REFRESH_TOKEN", "").strip().strip('"').strip("'")

    encoded = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    resp = requests.post(
        "https://api.login.yahoo.com/oauth2/get_token",
        headers={
            "Authorization": f"Basic {encoded}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "refresh_token",
            "redirect_uri": "https://localhost:8080",
            "refresh_token": refresh_token,
        },
        timeout=30,
    )
    print(f"Yahoo OAuth status {resp.status_code}: {resp.text}" if not resp.ok else "Yahoo OAuth token refreshed!")
    resp.raise_for_status()
    token_payload = resp.json()

    access_token = token_payload["access_token"]
    new_refresh = token_payload.get("refresh_token", refresh_token)
    guid = token_payload.get("xoauth_yahoo_guid", "")
    token_time = time.time()
    token_type = token_payload.get("token_type", "bearer")

    token_data = {
        "access_token": access_token,
        "consumer_key": client_id,
        "consumer_secret": client_secret,
        "guid": guid,
        "refresh_token": n        "token_time": token_time,
        "token_type": token_type,
    }
    (auth_dir / "token.json").write_text(json.dumps(token_data, indent=2))
    (auth_dir / "private.json").write_text(
        json.dumps({"consumer_key": client_id, "consumer_secret": client_secret}, indent=2)
    )
    env_lines = [
        f"YAHOO_CONSUMER_KEY={client_id}",
        f"YAHOO_CONSUMER_SECRET={client_secret}",
        f"YAHOO_ACCESS_TOKEN={access_token}",
        f"YAHOO_REFRESH_TOKEN={new_refresh}",
        f"YAHOO_GUID={guid}",
        f"YAHOO_TOKEN_TIME={token_time}",
        f"YAHOO_TOKEN_TYPE={token_type}",
    ]
    (auth_dir / ".env").write_text("\n".join(env_lines) + "\n")

def discover_sbhl_seasons(auth_dir: Path) -> dict[str, tuple[str, int]]:
    """Finds the seed SBHL league and walks Yahoo's renew/renewed chain across all past and future seasons."""
    discovered_seasons = {}
    queue = []
    visited = set()

    # 1. Resolve current/latest NHL game_id dynamically plus known NHL game_ids
    game_ids_to_try = []
    try:
        q_game = YahooFantasySportsQuery(
            league_id=SEED_LEAGUE_ID,
            game_code="nhl",
            env_file_location=auth_dir,
            save_token_data_to_env_file=True,
        )
        curr_gid = int(q_game.get_current_game_info().game_id)
        game_ids_to_try.append(curr_gid)
    except Exception:
        pass

    for gid in CANDIDATE_NHL_GAME_IDS:
        if gid not in game_ids_to_try:
            game_ids_to_try.append(gid)

    # 2. Locate the seed league without touching /users;use_login=1
    for gid in game_ids_to_try:
        try:
            q = YahooFantasySportsQuery(
                league_id=SEED_LEAGUE_ID,
                game_code="nhl",
                game_id=gid,
                env_file_location=auth_dir,
                save_token_data_to_env_file=True,
            )
            meta = q.get_league_metadata()
            raw_name = meta.name.decode("utf-8") if isinstance(meta.name, bytes) else str(meta.name)
            print(f"Found seed league '{raw_name}' in game_id {gid} (season {meta.season})")
            queue.append(f"{gid}_{SEED_LEAGUE_ID}")
            break
        except Exception:
            continue

    # 3. Traverse every linked past (`renew`) and future (`renewed`) SBHL season
    while queue:
        current_key = queue.pop(0)
        if current_key in visited or "_" not in current_key:
            continue
        visited.add(current_key)

        g_id_str, l_id_str = current_key.split("_", 1)
        try:
            q = YahooFantasySportsQuery(
                league_id=l_id_str,
                game_code="nhl",
                game_id=int(g_id_str),
                env_file_location=auth_dir,
                save_token_data_to_env_file=True,
            )
            meta = q.get_league_metadata()
            season_str = str(meta.season)
            discovered_seasons[season_str] = (l_id_str, int(g_id_str))

            for neighbor in (str(getattr(meta, "renew", "") or ""), str(getattr(meta, "renewed", "") or "")):
                if neighbor and "_" in neighbor and neighbor not in visited:
                    queue.append(neighbor)
        except Exception as exc:
            print(f"Skipping linked league {current_key}: {exc}")

    return dict(sorted(discovered_seasons.items(), key=lambda item: item[0]))

def build_hof_database():
    auth_dir = Path("./scripts")
    bootstrap_headless_token(auth_dir)

    seasons = discover_sbhl_seasons(auth_dir)
    print(f"Discovered {len(seasons)} SBHL seasons: {seasons}")

    managers = defaultdict(lambda: {
        "guid": "",
        "name": "",
        "latest_team_name": "",
        "wins": 0,
        "losses": 0,
        "ties": 0,
        "all_play_wins": 0,
        "all_play_losses": 0,
        "all_play_ties": 0,
        "titles": 0,
        "playoffs": 0,
        "seasons_played": 0,
        "finishes": {},
    })
    banners = []
    h2h_matrix = defaultdict(lambda: defaultdict(lambda: {"w": 0, "l": 0, "t": 0}))

    for year, (league_id, game_id) in seasons.items():
        print(f"Syncing SBHL {year} (League {league_id})...")
        q = YahooFantasySportsQuery(
            league_id=league_id,
            game_code="nhl",
            game_id=game_id,
            env_file_location=auth_dir,
            save_token_data_to_env_file=True,
        )

        league_info = q.get_league_info()
        current_week = int(getattr(league_info, "current_week", 1) or 1)
        end_week = int(getattr(league_info, "end_week", 24) or 24)
        max_week = min(current_week, end_week)

        standings = q.get_league_standings()
        team_to_guid = {}

        for team_wrapper in standings.teams:
            team = getattr(team_wrapper, "team", team_wrapper)
            team_key = str(team.team_key)
            team_name = team.name.decode("utf-8") if isinstance(team.name, bytes) else str(team.name)

            mgr_list = getattr(team, "managers", [])
            first_mgr = unwrap(mgr_list)
            mgr_obj = getattr(first_mgr, "manager", first_mgr)
            guid = str(getattr(mgr_obj, "guid", team_key))
            raw_nickname = str(getattr(mgr_obj, "nickname", team_name))
            mgr_name = MANAGER_NAME_MAP.get(guid, raw_nickname)

            team_to_guid[team_key] = guid
            m = managers[guid]
            m["guid"] = guid
            m["name"] = mgr_name
            m["latest_team_name"] = team_name
            m["seasons_played"] += 1

            ts = team.team_standings
            outcomes = ts.outcome_totals
            m["wins"] += int(outcomes.wins or 0)
            m["losses"] += int(outcomes.losses or 0)
            m["ties"] += int(outcomes.ties or 0)

            rank = int(ts.rank or 0) if getattr(ts, "rank", None) else 0
            if rank > 0:
                m["finishes"][year] = rank
            if getattr(ts, "playoff_seed", None):
                m["playoffs"] += 1
            if rank == 1 and int(getattr(league_info, "is_finished", 0) or 0) == 1:
                m["titles"] += 1
                banners.append({
                    "year": year,
                    "manager": mgr_name,
                    "team": team_name,
                    "record": f"{outcomes.wins}-{outcomes.losses}-{outcomes.ties}",
                })

        for wk in range(1, max_week + 1):
            try:
                matchups = q.get_league_matchups_by_week(wk)
            except Exception:
                continue

            weekly_scores = {}
            for m_wrap in matchups:
                matchup = getattr(m_wrap, "matchup", m_wrap)
                teams_in_m = getattr(matchup, "teams", [])
                if len(teams_in_m) != 2:
                    continue

                t1 = getattr(teams_in_m[0], "team", teams_in_m[0])
                t2 = getattr(teams_in_m[1], "team", teams_in_m[1])
                g1 = team_to_guid.get(str(t1.team_key))
                g2 = team_to_guid.get(str(t2.team_key))
                if not g1 or not g2:
                    continue

                s1 = float(getattr(t1.team_points, "total", 0.0) or 0.0)
                s2 = float(getattr(t2.team_points, "total", 0.0) or 0.0)
                if s1 == 0 and s2 == 0:
                    continue

                weekly_scores[g1] = s1
                weekly_scores[g2] = s2

                if s1 > s2:
                    h2h_matrix[g1][g2]["w"] += 1
                    h2h_matrix[g2][g1]["l"] += 1
                elif s2 > s1:
                    h2h_matrix[g2][g1]["w"] += 1
                    h2h_matrix[g1][g2]["l"] += 1
                else:
                    h2h_matrix[g1][g2]["t"] += 1
                    h2h_matrix[g2][g1]["t"] += 1

            guids = list(weekly_scores.keys())
            for i, ga in enumerate(guids):
                for gb in guids[i + 1:]:
                    if weekly_scores[ga] > weekly_scores[gb]:
                        managers[ga]["all_play_wins"] += 1
                        managers[gb]["all_play_losses"] += 1
                    elif weekly_scores[gb] > weekly_scores[ga]:
                        managers[gb]["all_play_wins"] += 1
                        managers[ga]["all_play_losses"] += 1
                    else:
                        managers[ga]["all_play_ties"] += 1
                        managers[gb]["all_play_ties"] += 1

    leaderboard = []
    for guid, m in managers.items():
        total_games = m["wins"] + m["losses"] + m["ties"]
        ap_games = m["all_play_wins"] + m["all_play_losses"] + m["all_play_ties"]
        win_pct = round((m["wins"] + 0.5 * m["ties"]) / total_games * 100, 1) if total_games else 0.0
        ap_pct = round((m["all_play_wins"] + 0.5 * m["all_play_ties"]) / ap_games * 100, 1) if ap_games else 0.0
        luck_diff = round(win_pct - ap_pct, 1)
        ranks = list(m["finishes"].values())
        avg_finish = round(sum(ranks) / len(ranks), 2) if ranks else 0.0

        leaderboard.append({
            **m,
            "win_pct": win_pct,
            "all_play_pct": ap_pct,
            "luck_rating": luck_diff,
            "avg_finish": avg_finish,
        })

    leaderboard.sort(key=lambda x: (x["titles"], x["win_pct"]), reverse=True)

    output = {
        "updated_at": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
        "seasons": list(seasons.keys()),
        "banners": sorted(banners, key=lambda x: x["year"], reverse=True),
        "leaderboard": leaderboard,
        "h2h": h2h_matrix,
    }

    Path("./public/data/sbhl_db.json").write_text(json.dumps(output, indent=2))
    print("Saved public/data/sbhl_db.json successfully!")

if __name__ == "__main__":
    build_hof_database()