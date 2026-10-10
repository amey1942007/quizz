#!/usr/bin/env python3
"""Local Kahoot-style quiz server.

The laptop is authoritative for timing, scoring, and admin login.
SQLite and CSV stay on disk. Supabase is an optional live leaderboard mirror.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import shutil
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
SERVER_DIR = Path(__file__).resolve().parent
PAGES_URL = "https://amey1942007.github.io/quizz/"


def load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_dotenv(SERVER_DIR / ".env")
load_dotenv(ROOT / ".env")

DATA_DIR = Path(os.environ.get("QUIZ_DATA_DIR", SERVER_DIR / "data"))
DB_PATH = DATA_DIR / "quiz.sqlite"
EXPORTS = DATA_DIR / "exports"
CONFIG = json.loads((SERVER_DIR / "config.json").read_text(encoding="utf-8"))

SB_URL = os.environ.get("SUPABASE_URL", "https://uvnmysivplivpvdcbbtp.supabase.co").rstrip("/")
SB_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()

PORT = 8000
WANT_TUNNEL = False

SCHEMA = """
create table if not exists questions (
  id integer primary key autoincrement,
  text text not null,
  options_json text not null,
  correct_index integer not null,
  time_limit integer not null
);
create table if not exists games (
  id text primary key,
  pin text unique not null,
  phase text not null,
  q_index integer not null,
  questions_json text not null,
  created_at text not null
);
create table if not exists players (
  id text primary key,
  game_id text not null,
  token text not null,
  nickname text not null,
  score integer not null,
  streak integer not null,
  total_time_ms integer not null,
  rank integer not null
);
create table if not exists answers (
  id text primary key,
  game_id text not null,
  player_id text not null,
  question_index integer not null,
  choice integer,
  correct integer not null,
  time_ms integer,
  points integer not null,
  question text not null,
  answer_text text not null
);
"""

conn: sqlite3.Connection | None = None
QUESTIONS: list[dict] = []
GAMES: dict[str, "Game"] = {}
SESSIONS: dict[str, float] = {}
ROOMS: dict[str, set["Conn"]] = {}
sb_client: httpx.AsyncClient | None = None
sync_queue: asyncio.Queue = asyncio.Queue()


def log(msg: str) -> None:
    print(msg, flush=True)


def check_password(username: str, password: str) -> bool:
    user_ok = hmac.compare_digest(
        hashlib.sha256(username.encode()).digest(),
        hashlib.sha256(CONFIG["admin_username"].encode()).digest(),
    )
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode(),
        CONFIG["password_salt"].encode(),
        int(CONFIG["password_iterations"]),
    ).hex()
    return user_ok and hmac.compare_digest(digest, CONFIG["password_hash"])


def init_db() -> None:
    global conn
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    EXPORTS.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    load_questions()


def load_questions() -> None:
    global QUESTIONS
    assert conn is not None
    QUESTIONS = []
    for row in conn.execute("select * from questions order by id"):
        QUESTIONS.append(
            {
                "id": row["id"],
                "text": row["text"],
                "options": json.loads(row["options_json"]),
                "correct_index": row["correct_index"],
                "time_limit": row["time_limit"],
            }
        )


def require_conn() -> sqlite3.Connection:
    if conn is None:
        init_db()
    assert conn is not None
    return conn


class QuestionIn(BaseModel):
    text: str
    options: list[str]
    correct_index: int
    time_limit: int = 20


class GameIn(BaseModel):
    question_ids: list[int] = Field(default_factory=list)


class LoginIn(BaseModel):
    username: str
    password: str


class JoinIn(BaseModel):
    pin: str
    nickname: str


class AnswerIn(BaseModel):
    pin: str
    token: str
    choice: int


class Player:
    def __init__(self, pid: str, token: str, nickname: str):
        self.id = pid
        self.token = token
        self.nickname = nickname
        self.score = 0
        self.streak = 0
        self.total_time_ms = 0
        self.rank = 1
        self.last_points = 0
        self.last_correct: bool | None = None


class Game:
    def __init__(self, gid: str, pin: str, questions: list[dict]):
        self.id = gid
        self.pin = pin
        self.questions = questions
        self.phase = "lobby"
        self.index = -1
        self.started_at = 0.0
        self.deadline = 0.0
        self.players: list[Player] = []
        self.by_token: dict[str, Player] = {}
        self.answers: dict[tuple[str, int], dict] = {}
        self.lock = asyncio.Lock()
        self.created_at = time.time()


class Conn:
    def __init__(self, ws: WebSocket, token: str, role: str, pin: str):
        self.ws = ws
        self.token = token
        self.role = role
        self.pin = pin


def clean_question(body: QuestionIn) -> tuple[str, list[str], int, int]:
    text = body.text.strip()
    if not text or len(text) > 300:
        raise HTTPException(400, "Question text is required")
    options = [item.strip() for item in body.options if item and item.strip()]
    if not 2 <= len(options) <= 4:
        raise HTTPException(400, "Provide 2 to 4 options")
    if len(options) != len(set(item.casefold() for item in options)):
        raise HTTPException(400, "Options must be different")
    if not 0 <= body.correct_index < len(options):
        raise HTTPException(400, "Mark a correct option")
    limit = body.time_limit or 20
    if not 5 <= limit <= 120:
        raise HTTPException(400, "Time limit must be 5 to 120 seconds")
    for option in options:
        if len(option) > 120:
            raise HTTPException(400, "Options must be 120 characters or fewer")
    return text, options, body.correct_index, limit


def admin_or_401(authorization: str | None, token: str | None = None) -> str:
    raw = token or ""
    if authorization and authorization.lower().startswith("bearer "):
        raw = authorization.split(" ", 1)[1].strip()
    exp = SESSIONS.get(raw)
    if not raw or not exp or exp < time.time():
        SESSIONS.pop(raw, None)
        raise HTTPException(401, "Login required")
    return raw


def new_pin() -> str:
    for _ in range(30):
        pin = f"{secrets.randbelow(900000) + 100000}"
        if pin not in GAMES:
            return pin
    raise HTTPException(500, "Could not allocate a PIN")


def snapshot_question(row: dict) -> dict:
    return {
        "text": row["text"],
        "options": list(row["options"]),
        "correct_index": row["correct_index"],
        "time_limit": row["time_limit"],
    }


def recompute_ranks(game: Game) -> None:
    ordered = sorted(game.players, key=lambda p: (-p.score, p.total_time_ms, p.nickname.casefold()))
    last = None
    rank = 0
    for index, player in enumerate(ordered, start=1):
        marker = (player.score, player.total_time_ms)
        if marker != last:
            rank = index
            last = marker
        player.rank = rank


def leaderboard(game: Game) -> list[dict]:
    rows = sorted(game.players, key=lambda p: (p.rank, p.nickname.casefold()))
    return [
        {
            "player_id": p.id,
            "nickname": p.nickname,
            "score": p.score,
            "rank": p.rank,
            "streak": p.streak,
            "last_points": p.last_points,
        }
        for p in rows
    ]


def distribution(game: Game) -> list[int]:
    question = game.questions[game.index]
    counts = [0] * len(question["options"])
    for (pid, idx), answer in game.answers.items():
        if idx == game.index and answer["choice"] is not None:
            counts[answer["choice"]] += 1
    return counts


def question_block(game: Game, reveal: bool) -> dict | None:
    if game.index < 0:
        return None
    question = game.questions[game.index]
    block = {
        "index": game.index,
        "total": len(game.questions),
        "text": question["text"],
        "options": question["options"],
        "time_limit": question["time_limit"],
        "ends_at": int(game.deadline * 1000),
        "server_now": int(time.time() * 1000),
    }
    if reveal:
        block["correct_index"] = question["correct_index"]
        block["distribution"] = distribution(game)
    return block


def answers_in(game: Game) -> int:
    if game.index < 0:
        return 0
    return sum(1 for player in game.players if (player.id, game.index) in game.answers)


def admin_view(game: Game) -> dict:
    reveal = game.phase in ("reveal", "podium")
    return {
        "role": "admin",
        "game_id": game.id,
        "pin": game.pin,
        "phase": game.phase,
        "question_index": game.index,
        "question_count": len(game.questions),
        "question": question_block(game, reveal),
        "players": [
            {"player_id": p.id, "nickname": p.nickname, "score": p.score, "rank": p.rank}
            for p in game.players
        ],
        "leaderboard": leaderboard(game),
        "answers_in": answers_in(game),
        "player_count": len(game.players),
    }


def player_view(game: Game, player: Player) -> dict:
    reveal = game.phase in ("reveal", "podium")
    current = game.answers.get((player.id, game.index)) if game.index >= 0 else None
    return {
        "role": "player",
        "game_id": game.id,
        "pin": game.pin,
        "phase": game.phase,
        "question_index": game.index,
        "question_count": len(game.questions),
        "question": question_block(game, reveal),
        "leaderboard": leaderboard(game),
        "answers_in": answers_in(game),
        "player_count": len(game.players),
        "you": {
            "player_id": player.id,
            "nickname": player.nickname,
            "score": player.score,
            "rank": player.rank,
            "streak": player.streak,
            "answered": current is not None and game.phase == "question",
            "choice": current["choice"] if current else None,
            "last_points": player.last_points if reveal else None,
            "last_correct": player.last_correct if reveal else None,
        },
    }


def begin_question(game: Game) -> None:
    game.phase = "question"
    game.started_at = time.time()
    limit = game.questions[game.index]["time_limit"]
    game.deadline = game.started_at + limit
    for player in game.players:
        player.last_points = 0
        player.last_correct = None


def all_answered(game: Game) -> bool:
    return bool(game.players) and all((p.id, game.index) in game.answers for p in game.players)


def close_question(game: Game) -> None:
    if game.phase != "question" or game.index < 0:
        return
    question = game.questions[game.index]
    for player in game.players:
        key = (player.id, game.index)
        if key in game.answers:
            continue
        player.streak = 0
        player.last_points = 0
        player.last_correct = False
        game.answers[key] = {
            "id": f"{game.id}:{player.id}:{game.index}",
            "player_id": player.id,
            "question_index": game.index,
            "choice": None,
            "correct": False,
            "time_ms": None,
            "points": 0,
            "question": question["text"],
            "answer_text": "",
        }
    game.phase = "reveal"
    recompute_ranks(game)


def build_csv(game: Game) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["player", "question_number", "question", "answer", "correct", "time_ms", "points", "total"])
    last = game.index if game.phase != "lobby" else -1
    for player in sorted(game.players, key=lambda item: item.nickname.casefold()):
        for index in range(last + 1):
            answer = game.answers.get((player.id, index))
            if answer is None:
                continue
            writer.writerow(
                [
                    player.nickname,
                    index + 1,
                    answer["question"],
                    answer["answer_text"],
                    "yes" if answer["correct"] else "no",
                    "" if answer["time_ms"] is None else answer["time_ms"],
                    answer["points"],
                    player.score,
                ]
            )
    return buffer.getvalue()


def write_csv(game: Game) -> None:
    text = build_csv(game)
    (EXPORTS / f"quiz_{game.pin}.csv").write_text(text, encoding="utf-8")
    if game.phase == "podium":
        stamp = time.strftime("%Y%m%d-%H%M%S")
        (EXPORTS / f"quiz_{game.pin}_{stamp}.csv").write_text(text, encoding="utf-8")


def save_game(game: Game) -> None:
    db = require_conn()
    db.execute(
        """
        insert into games (id, pin, phase, q_index, questions_json, created_at)
        values (?, ?, ?, ?, ?, ?)
        on conflict(id) do update set
          phase=excluded.phase,
          q_index=excluded.q_index,
          questions_json=excluded.questions_json
        """,
        (
            game.id,
            game.pin,
            game.phase,
            game.index,
            json.dumps(game.questions),
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(game.created_at)),
        ),
    )
    db.execute("delete from players where game_id = ?", (game.id,))
    db.execute("delete from answers where game_id = ?", (game.id,))
    db.executemany(
        """
        insert into players (id, game_id, token, nickname, score, streak, total_time_ms, rank)
        values (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (p.id, game.id, p.token, p.nickname, p.score, p.streak, p.total_time_ms, p.rank)
            for p in game.players
        ],
    )
    db.executemany(
        """
        insert into answers (
          id, game_id, player_id, question_index, choice, correct, time_ms, points, question, answer_text
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                answer["id"],
                game.id,
                answer["player_id"],
                answer["question_index"],
                answer["choice"],
                1 if answer["correct"] else 0,
                answer["time_ms"],
                answer["points"],
                answer["question"],
                answer["answer_text"],
            )
            for answer in game.answers.values()
        ],
    )
    db.commit()


def sync_snapshot(game: Game) -> dict:
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return {
        "game": {
            "id": game.id,
            "pin": game.pin,
            "phase": game.phase,
            "question_count": len(game.questions),
            "current_index": game.index,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(game.created_at)),
            "updated_at": now,
        },
        "players": [
            {
                "id": p.id,
                "game_id": game.id,
                "nickname": p.nickname,
                "score": p.score,
                "streak": p.streak,
                "joined_at": now,
            }
            for p in game.players
        ],
        "answers": [
            {
                "id": answer["id"],
                "game_id": game.id,
                "player_id": answer["player_id"],
                "question_index": answer["question_index"],
                "choice": answer["choice"],
                "correct": bool(answer["correct"]),
                "time_ms": answer["time_ms"],
                "points": answer["points"],
                "created_at": now,
            }
            for answer in game.answers.values()
        ],
        "scores": [
            {
                "game_id": game.id,
                "player_id": p.id,
                "nickname": p.nickname,
                "score": p.score,
                "rank": p.rank,
                "streak": p.streak,
                "last_points": p.last_points,
                "updated_at": now,
            }
            for p in game.players
        ],
    }


def enqueue_sync(snap: dict) -> None:
    if not SB_KEY:
        return
    sync_queue.put_nowait(snap)


async def push_snapshot(snap: dict) -> None:
    if sb_client is None or not SB_KEY:
        return

    async def upsert(table: str, rows: list[dict] | dict, conflict: str) -> None:
        if not rows:
            return
        response = await sb_client.post(
            f"{SB_URL}/rest/v1/{table}",
            params={"on_conflict": conflict},
            headers={
                "apikey": SB_KEY,
                "Authorization": f"Bearer {SB_KEY}",
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates,return=minimal",
            },
            json=rows,
        )
        if response.status_code >= 300:
            log(f"supabase {table} failed: {response.status_code} {response.text[:240]}")

    await upsert("quiz_games", snap["game"], "id")
    await upsert("quiz_players", snap["players"], "id")
    await upsert("quiz_answers", snap["answers"], "id")
    await upsert("quiz_scores", snap["scores"], "game_id,player_id")


async def sync_worker() -> None:
    while True:
        snap = await sync_queue.get()
        while not sync_queue.empty():
            try:
                snap = sync_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        try:
            await push_snapshot(snap)
        except Exception as exc:
            log(f"supabase sync failed: {exc}")


def persist(game: Game) -> dict:
    recompute_ranks(game)
    save_game(game)
    write_csv(game)
    return sync_snapshot(game)


def apply_answer(game: Game, token: str, choice: int) -> dict:
    if game.phase != "question" or game.index < 0:
        raise HTTPException(409, "Question is not open")
    player = game.by_token.get(token)
    if player is None:
        raise HTTPException(401, "Not in this game")
    if (player.id, game.index) in game.answers:
        raise HTTPException(409, "Already answered")
    question = game.questions[game.index]
    if choice < 0 or choice >= len(question["options"]):
        raise HTTPException(400, "Invalid choice")
    now = time.time()
    if now > game.deadline + 0.25:
        raise HTTPException(409, "Too late")
    elapsed = min(max(now - game.started_at, 0.0), float(question["time_limit"]))
    time_ms = int(round(elapsed * 1000))
    correct = choice == question["correct_index"]
    if correct:
        player.streak += 1
        base = int(round(1000 * (1 - (elapsed / question["time_limit"]) / 2)))
        bonus = min(500, 100 * (player.streak - 1))
        points = base + bonus
        player.total_time_ms += time_ms
    else:
        player.streak = 0
        points = 0
    player.score += points
    player.last_points = points
    player.last_correct = correct
    game.answers[(player.id, game.index)] = {
        "id": f"{game.id}:{player.id}:{game.index}",
        "player_id": player.id,
        "question_index": game.index,
        "choice": choice,
        "correct": correct,
        "time_ms": time_ms,
        "points": points,
        "question": question["text"],
        "answer_text": question["options"][choice],
    }
    if all_answered(game):
        close_question(game)
    return {"ok": True, "time_ms": time_ms}


async def broadcast(game: Game) -> None:
    async with game.lock:
        admin_payload = admin_view(game)
        player_payloads = {p.token: player_view(game, p) for p in game.players}
        conns = list(ROOMS.get(game.pin, set()))
    dead: list[Conn] = []
    for item in conns:
        try:
            if item.role == "admin":
                payload = admin_payload
            else:
                payload = player_payloads.get(item.token)
                if payload is None:
                    continue
            await item.ws.send_json({"type": "state", "state": payload})
        except Exception:
            dead.append(item)
    room = ROOMS.get(game.pin)
    if room:
        for item in dead:
            room.discard(item)


async def ticker() -> None:
    while True:
        await asyncio.sleep(0.2)
        due: list[Game] = []
        for game in list(GAMES.values()):
            async with game.lock:
                if game.phase == "question" and time.time() >= game.deadline:
                    close_question(game)
                    snap = persist(game)
                    due.append(game)
                else:
                    snap = None
            if snap is not None:
                enqueue_sync(snap)
        for game in due:
            await broadcast(game)


def print_links(server_base: str) -> None:
    encoded = quote(server_base, safe="")
    log("=" * 64)
    log(f"Server:  {server_base}")
    log(f"Players: {PAGES_URL}?server={encoded}")
    log(f"Admin:   {PAGES_URL}?server={encoded}#host")
    log("=" * 64)


async def run_tunnel(port: int) -> None:
    exe = shutil.which("cloudflared")
    if not exe:
        log("cloudflared not found. Install with: winget install Cloudflare.cloudflared")
        log("Continuing on localhost only.")
        return
    proc = await asyncio.create_subprocess_exec(
        exe,
        "tunnel",
        "--url",
        f"http://127.0.0.1:{port}",
        "--no-autoupdate",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    assert proc.stdout is not None
    announced = False
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        text = line.decode(errors="replace").rstrip()
        log(text)
        if announced:
            continue
        match = re.search(r"https://[0-9a-zA-Z-]+\.trycloudflare\.com", text)
        if match:
            announced = True
            print_links(match.group(0))


@asynccontextmanager
async def lifespan(app: FastAPI):
    global sb_client
    init_db()
    tasks = [asyncio.create_task(ticker())]
    if SB_KEY:
        sb_client = httpx.AsyncClient(timeout=8)
        tasks.append(asyncio.create_task(sync_worker()))
        log("Supabase mirror: enabled")
    else:
        log("Supabase mirror: disabled (local scoring still works)")
    if WANT_TUNNEL:
        tasks.append(asyncio.create_task(run_tunnel(PORT)))
    print_links(f"http://127.0.0.1:{PORT}")
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        if sb_client is not None:
            await sb_client.aclose()


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://amey1942007.github.io"],
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?|https://.*\.trycloudflare\.com",
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
async def health():
    return {"ok": True, "supabase": bool(SB_KEY)}


@app.post("/api/admin/login")
async def login(body: LoginIn):
    if not check_password(body.username.strip(), body.password):
        raise HTTPException(401, "Wrong username or password")
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = time.time() + 12 * 3600
    return {"token": token}


@app.get("/api/admin/questions")
async def list_questions(authorization: str | None = Header(None)):
    admin_or_401(authorization)
    return {"questions": QUESTIONS}


@app.post("/api/admin/questions")
async def create_question(body: QuestionIn, authorization: str | None = Header(None)):
    admin_or_401(authorization)
    text, options, correct, limit = clean_question(body)
    db = require_conn()
    cur = db.execute(
        "insert into questions (text, options_json, correct_index, time_limit) values (?, ?, ?, ?)",
        (text, json.dumps(options), correct, limit),
    )
    db.commit()
    item = {
        "id": cur.lastrowid,
        "text": text,
        "options": options,
        "correct_index": correct,
        "time_limit": limit,
    }
    QUESTIONS.append(item)
    return item


@app.put("/api/admin/questions/{question_id}")
async def update_question(question_id: int, body: QuestionIn, authorization: str | None = Header(None)):
    admin_or_401(authorization)
    text, options, correct, limit = clean_question(body)
    db = require_conn()
    cur = db.execute(
        "update questions set text=?, options_json=?, correct_index=?, time_limit=? where id=?",
        (text, json.dumps(options), correct, limit, question_id),
    )
    db.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "Question not found")
    item = {
        "id": question_id,
        "text": text,
        "options": options,
        "correct_index": correct,
        "time_limit": limit,
    }
    for index, existing in enumerate(QUESTIONS):
        if existing["id"] == question_id:
            QUESTIONS[index] = item
            break
    return item


@app.delete("/api/admin/questions/{question_id}")
async def delete_question(question_id: int, authorization: str | None = Header(None)):
    admin_or_401(authorization)
    db = require_conn()
    cur = db.execute("delete from questions where id=?", (question_id,))
    db.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "Question not found")
    QUESTIONS[:] = [item for item in QUESTIONS if item["id"] != question_id]
    return {"ok": True}


@app.post("/api/admin/games")
async def create_game(body: GameIn, authorization: str | None = Header(None)):
    admin_or_401(authorization)
    ids = body.question_ids or [item["id"] for item in QUESTIONS]
    by_id = {item["id"]: item for item in QUESTIONS}
    picked = []
    for qid in ids:
        if qid not in by_id:
            raise HTTPException(400, f"Unknown question {qid}")
        picked.append(snapshot_question(by_id[qid]))
    if not picked:
        raise HTTPException(400, "Add at least one question first")
    game = Game(str(uuid.uuid4()), new_pin(), picked)
    GAMES[game.pin] = game
    ROOMS.setdefault(game.pin, set())
    async with game.lock:
        snap = persist(game)
    enqueue_sync(snap)
    await broadcast(game)
    return {"pin": game.pin, "game_id": game.id}


async def mutate(pin: str, authorization: str | None, action):
    admin_or_401(authorization)
    game = GAMES.get(pin)
    if game is None:
        raise HTTPException(404, "Game not found")
    async with game.lock:
        action(game)
        snap = persist(game)
    enqueue_sync(snap)
    await broadcast(game)
    return admin_view(game)


@app.post("/api/admin/games/{pin}/start")
async def start_game(pin: str, authorization: str | None = Header(None)):
    def action(game: Game) -> None:
        if game.phase != "lobby":
            raise HTTPException(409, "Game already started")
        if not game.questions:
            raise HTTPException(400, "This game has no questions")
        game.index = 0
        begin_question(game)

    return await mutate(pin, authorization, action)


@app.post("/api/admin/games/{pin}/next")
async def next_step(pin: str, authorization: str | None = Header(None)):
    def action(game: Game) -> None:
        if game.phase == "question":
            close_question(game)
        elif game.phase == "reveal":
            if game.index + 1 < len(game.questions):
                game.index += 1
                begin_question(game)
            else:
                game.phase = "podium"
        elif game.phase == "podium":
            return
        else:
            raise HTTPException(409, "Start the game first")

    return await mutate(pin, authorization, action)


@app.post("/api/admin/games/{pin}/end")
async def end_game(pin: str, authorization: str | None = Header(None)):
    def action(game: Game) -> None:
        if game.phase == "question":
            close_question(game)
        game.phase = "podium"

    return await mutate(pin, authorization, action)


@app.post("/api/join")
async def join(body: JoinIn):
    pin = body.pin.strip()
    nickname = " ".join(body.nickname.split())
    if not re.fullmatch(r"\d{6}", pin):
        raise HTTPException(400, "PIN must be 6 digits")
    game = GAMES.get(pin)
    if game is None:
        raise HTTPException(404, "Game not found")
    if not nickname or len(nickname) > 16:
        raise HTTPException(400, "Nickname must be 1 to 16 characters")
    async with game.lock:
        if game.phase != "lobby":
            raise HTTPException(409, "Game already started")
        if any(player.nickname.casefold() == nickname.casefold() for player in game.players):
            raise HTTPException(409, "Nickname already taken")
        player = Player(str(uuid.uuid4()), secrets.token_urlsafe(24), nickname)
        game.players.append(player)
        game.by_token[player.token] = player
        recompute_ranks(game)
        snap = persist(game)
    enqueue_sync(snap)
    await broadcast(game)
    return {"token": player.token, "player_id": player.id, "game_id": game.id, "pin": game.pin}


@app.post("/api/answer")
async def answer(body: AnswerIn):
    game = GAMES.get(body.pin.strip())
    if game is None:
        raise HTTPException(404, "Game not found")
    async with game.lock:
        result = apply_answer(game, body.token, body.choice)
        snap = persist(game)
        view = player_view(game, game.by_token[body.token])
    enqueue_sync(snap)
    await broadcast(game)
    result["score"] = view["you"]["score"]
    result["rank"] = view["you"]["rank"]
    result["phase"] = view["phase"]
    return result


@app.get("/api/state")
async def state(pin: str, token: str):
    game = GAMES.get(pin)
    if game is None:
        raise HTTPException(404, "Game not found")
    async with game.lock:
        if token in SESSIONS and SESSIONS[token] > time.time():
            return admin_view(game)
        player = game.by_token.get(token)
        if player is None:
            raise HTTPException(401, "Not allowed")
        return player_view(game, player)


@app.get("/export/{pin}")
async def export_csv(pin: str, authorization: str | None = Header(None), token: str | None = None):
    admin_or_401(authorization, token)
    game = GAMES.get(pin)
    if game is None:
        raise HTTPException(404, "Game not found")
    async with game.lock:
        text = build_csv(game)
        write_csv(game)
    return Response(
        content=text,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="quiz_{pin}.csv"'},
    )


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    pin = ws.query_params.get("pin", "")
    token = ws.query_params.get("token", "")
    role = ws.query_params.get("role", "player")
    game = GAMES.get(pin)
    if game is None:
        await ws.send_json({"type": "error", "detail": "Game not found"})
        await ws.close(code=1008)
        return
    if role == "admin":
        if token not in SESSIONS or SESSIONS[token] < time.time():
            await ws.send_json({"type": "error", "detail": "Login required"})
            await ws.close(code=1008)
            return
    elif token not in game.by_token:
        await ws.send_json({"type": "error", "detail": "Not in this game"})
        await ws.close(code=1008)
        return
    item = Conn(ws, token, "admin" if role == "admin" else "player", pin)
    ROOMS.setdefault(pin, set()).add(item)
    try:
        await broadcast(game)
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        ROOMS.get(pin, set()).discard(item)


app.mount("/", StaticFiles(directory=ROOT / "docs", html=True), name="site")


def main() -> None:
    global PORT, WANT_TUNNEL
    parser = argparse.ArgumentParser(description="Run the local live quiz server")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--tunnel", action="store_true", help="Start a Cloudflare quick tunnel")
    args = parser.parse_args()
    PORT = args.port
    WANT_TUNNEL = args.tunnel
    import uvicorn

    uvicorn.run(app, host=args.host, port=PORT, log_level="info")


if __name__ == "__main__":
    main()
