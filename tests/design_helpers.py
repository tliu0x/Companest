import asyncio


def _make_team(base, team_id, pi, *, role="general", extra="", pi_md="",
               team_soul=None, pi_soul="soul"):
    d = base / "teams" / team_id
    (d / "memory").mkdir(parents=True)
    (d / "pis" / pi).mkdir(parents=True)
    (d / "team.md").write_text(
        f"# Team: {team_id}\n- role: {role}\n- lead_pi: {pi}\n- enabled: true\n{extra}{pi_md}"
    )
    (d / "pis" / pi / "soul.md").write_text(pi_soul)
    if team_soul:
        (d / "soul.md").write_text(team_soul)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)
