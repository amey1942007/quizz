"""One smoke test: server starts, a player joins, answers, leaderboard updates."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"


def request(method, path, body=None, token=None):
    data = None if body is None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as res:
            raw = res.read().decode()
            return res.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        raise SystemExit(f"{method} {path} -> {exc.code} {raw}") from exc


def main():
    data_dir = tempfile.mkdtemp(prefix="quizsmoke-")
    env = os.environ.copy()
    env["QUIZ_DATA_DIR"] = data_dir
    env["SUPABASE_SERVICE_ROLE_KEY"] = ""
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "server", "server.py"), "--port", str(PORT)],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    logs = []

    def drain():
        for line in proc.stdout:
            logs.append(line.rstrip())

    threading.Thread(target=drain, daemon=True).start()
    try:
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                status, health = request("GET", "/api/health")
                if status == 200 and health.get("ok"):
                    break
            except Exception:
                time.sleep(0.2)
        else:
            print("\n".join(logs[-40:]))
            raise SystemExit("server did not start")

        _, login = request("POST", "/api/admin/login", {"username": "robo", "password": "orientation"})
        admin = login["token"]
        _, question = request(
            "POST",
            "/api/admin/questions",
            {"text": "2+2", "options": ["3", "4"], "correct_index": 1, "time_limit": 20},
            admin,
        )
        _, game = request("POST", "/api/admin/games", {"question_ids": [question["id"]]}, admin)
        _, player = request("POST", "/api/join", {"pin": game["pin"], "nickname": "Ada"})
        request("POST", f"/api/admin/games/{game['pin']}/start", {}, admin)
        request("POST", "/api/answer", {"pin": game["pin"], "token": player["token"], "choice": 1})
        _, state = request("GET", f"/api/state?pin={game['pin']}&token={admin}")
        board = state.get("leaderboard") or []
        if not board or board[0]["nickname"] != "Ada" or board[0]["score"] <= 0:
            raise SystemExit(f"leaderboard did not update: {state}")
        if board[0]["rank"] != 1:
            raise SystemExit(f"rank missing: {board}")
        print(f"smoke ok pin={game['pin']} score={board[0]['score']} phase={state['phase']}")
    except Exception:
        print("\n".join(logs[-60:]))
        raise
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    main()
