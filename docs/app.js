const cfg = window.QUIZ_CONFIG || {};
const app = document.getElementById("app");

let server = initialServer();
let screen = server ? (location.hash === "#host" ? "login" : "home") : "setup";
let adminToken = localStorage.getItem("quiz_admin") || "";
let playerToken = localStorage.getItem("quiz_player") || "";
let playerId = localStorage.getItem("quiz_player_id") || "";
let pin = localStorage.getItem("quiz_pin") || "";
let state = null;
let questions = [];
let error = "";
let socket = null;
let sending = false;
let clockOffset = 0;
let sb = null;
let channel = null;
let liveGameId = "";
const liveMap = new Map();
let renderStamp = "";

function initialServer() {
  const query = new URLSearchParams(location.search).get("server");
  if (query) {
    const clean = query.replace(/\/$/, "");
    localStorage.setItem("quiz_server", clean);
    return clean;
  }
  const saved = localStorage.getItem("quiz_server");
  if (saved) return saved;
  if (location.protocol.startsWith("http") && location.hostname !== "github.io" && !location.hostname.endsWith(".github.io")) {
    return location.origin;
  }
  return "";
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[ch]));
}

function serverUrl() {
  return (server || "").replace(/\/$/, "");
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (options.body && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
  if (adminToken && options.auth !== false) headers.Authorization = "Bearer " + adminToken;
  const response = await fetch(serverUrl() + path, { ...options, headers });
  const text = await response.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { detail: text }; }
  if (!response.ok) {
    const detail = data && data.detail;
    throw new Error(typeof detail === "string" ? detail : "Request failed");
  }
  return data;
}

function noteClock(next) {
  if (next && next.question && next.question.server_now) {
    clockOffset = next.question.server_now - Date.now();
  }
}

function applyState(next) {
  state = next;
  noteClock(next);
  if (next && next.game_id) startRealtime(next.game_id);
  const stamp = [screen, next && next.phase, next && next.question_index, next && next.pin, sending].join("|");
  if (stamp !== renderStamp) render();
  else paintLive();
}

function mergedBoard() {
  const base = new Map((state && state.leaderboard ? state.leaderboard : []).map((row) => [row.player_id, row]));
  for (const [id, row] of liveMap) base.set(id, { ...base.get(id), ...row });
  return [...base.values()].sort((a, b) => (a.rank || 999) - (b.rank || 999) || (b.score || 0) - (a.score || 0));
}

function visibleBoard() {
  const rows = mergedBoard();
  if (state && state.phase === "podium") return rows;
  return rows.slice(0, 5);
}

function supabaseReady() {
  return Boolean(cfg.SUPABASE_URL && cfg.SUPABASE_ANON_KEY && window.supabase && window.supabase.createClient);
}

function startRealtime(gameId) {
  if (!supabaseReady() || !gameId || liveGameId === gameId) return;
  liveGameId = gameId;
  try {
    if (!sb) sb = window.supabase.createClient(cfg.SUPABASE_URL, cfg.SUPABASE_ANON_KEY);
    if (channel) sb.removeChannel(channel);
    liveMap.clear();
    channel = sb.channel("quiz-leaderboard-" + gameId)
      .on("postgres_changes", {
        event: "*",
        schema: "public",
        table: "quiz_scores",
        filter: "game_id=eq." + gameId
      }, (payload) => {
        if (payload.new && payload.new.player_id) {
          liveMap.set(payload.new.player_id, payload.new);
          paintLive();
        }
      })
      .subscribe();
    sb.from("quiz_leaderboard").select("*").eq("game_id", gameId).order("rank")
      .then(({ data, error: sbError }) => {
        if (sbError || !data) return;
        data.forEach((row) => liveMap.set(row.player_id, row));
        paintLive();
      });
  } catch (err) {
    console.warn("Supabase realtime unavailable", err);
  }
}

function connectSocket(role, token) {
  if (socket) {
    socket.onclose = null;
    socket.close();
  }
  const base = serverUrl().replace(/^http/, "ws");
  const url = `${base}/ws?pin=${encodeURIComponent(pin)}&token=${encodeURIComponent(token)}&role=${role}`;
  const sock = new WebSocket(url);
  socket = sock;
  sock.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    if (msg.type === "state") applyState(msg.state);
  };
  sock.onclose = (event) => {
    if (socket !== sock || event.code === 1008) return;
    setTimeout(() => {
      if (screen === "host" || screen === "play") connectSocket(role, token);
    }, 1500);
  };
}

async function refresh() {
  if (!pin || (screen !== "host" && screen !== "play")) return;
  const token = screen === "host" ? adminToken : playerToken;
  const next = await api("/api/state?pin=" + encodeURIComponent(pin) + "&token=" + encodeURIComponent(token), { auth: false });
  applyState(next);
}

function paintLive() {
  const timer = document.getElementById("timer");
  const bar = document.getElementById("bar");
  if (timer && state && state.phase === "question" && state.question) {
    const left = Math.max(0, state.question.ends_at - (Date.now() + clockOffset));
    timer.textContent = Math.ceil(left / 1000) + "s";
    if (bar) bar.style.width = Math.max(0, Math.min(100, (left / (state.question.time_limit * 1000)) * 100)) + "%";
  }
  const count = document.getElementById("count");
  if (count && state) count.textContent = `${state.answers_in || 0} / ${state.player_count || 0} answered`;
  const players = document.getElementById("players");
  if (players && state) {
    players.innerHTML = (state.players || []).map((p) => `<li>${esc(p.nickname)}</li>`).join("") || "<li>Waiting for players…</li>";
  }
  const board = document.getElementById("board");
  if (board && state) {
    const mine = (state.you && state.you.player_id) || playerId;
    const rows = visibleBoard();
    board.innerHTML = rows.map((row) => {
      const mark = row.player_id === mine ? " me" : "";
      return `<li class="${mark}">${esc(row.nickname)} — ${esc(row.score)} pts <span class="muted">#${esc(row.rank)}</span></li>`;
    }).join("") || "<li>No scores yet</li>";
  }
}

function shell(title, body) {
  app.innerHTML = `
    <div class="top"><h1>${title}</h1><button id="home-btn" type="button">Home</button></div>
    ${error ? `<p class="error">${esc(error)}</p>` : ""}
    ${body}`;
  const home = document.getElementById("home-btn");
  if (home) home.onclick = () => { error = ""; screen = "home"; render(); };
}

function render() {
  renderStamp = [screen, state && state.phase, state && state.question_index, state && state.pin, sending].join("|");
  if (screen === "setup") return renderSetup();
  if (screen === "home") return renderHome();
  if (screen === "login") return renderLogin();
  if (screen === "editor") return renderEditor();
  if (screen === "join") return renderJoin();
  if (screen === "host") return renderHost();
  if (screen === "play") return renderPlay();
  renderHome();
}

function renderSetup() {
  app.innerHTML = `
    <h1>Live Quiz</h1>
    <div class="card stack">
      <label>Server URL<input id="server" placeholder="https://….trycloudflare.com" value="${esc(server)}"></label>
      <button id="save" type="button">Continue</button>
      <p class="muted">Paste the https address printed by the laptop server. It is saved on this phone.</p>
    </div>`;
  document.getElementById("save").onclick = () => {
    server = document.getElementById("server").value.trim().replace(/\/$/, "");
    if (!server) { error = "Enter the server URL"; render(); return; }
    localStorage.setItem("quiz_server", server);
    error = "";
    screen = location.hash === "#host" ? "login" : "home";
    render();
  };
}

function renderHome() {
  app.innerHTML = `
    <h1>Live Quiz</h1>
    ${error ? `<p class="error">${esc(error)}</p>` : ""}
    <div class="card stack">
      <label>Server URL<input id="server" value="${esc(server)}"></label>
      <div class="row">
        <button id="join" type="button">Join game</button>
        <button id="host" type="button">Host</button>
      </div>
    </div>`;
  document.getElementById("join").onclick = () => {
    server = document.getElementById("server").value.trim().replace(/\/$/, "");
    localStorage.setItem("quiz_server", server);
    error = "";
    screen = "join";
    render();
  };
  document.getElementById("host").onclick = () => {
    server = document.getElementById("server").value.trim().replace(/\/$/, "");
    localStorage.setItem("quiz_server", server);
    error = "";
    screen = adminToken ? "editor" : "login";
    if (adminToken) loadQuestions().catch((err) => { error = err.message; screen = "login"; render(); });
    else render();
  };
}

function renderLogin() {
  shell("Host login", `
    <form id="form" class="card stack">
      <label>Username<input name="username" autocomplete="username" required></label>
      <label>Password<input name="password" type="password" autocomplete="current-password" required></label>
      <button type="submit">Log in</button>
    </form>`);
  document.getElementById("form").onsubmit = async (event) => {
    event.preventDefault();
    const form = event.target;
    try {
      const data = await api("/api/admin/login", {
        method: "POST",
        auth: false,
        body: JSON.stringify({ username: form.username.value, password: form.password.value })
      });
      adminToken = data.token;
      localStorage.setItem("quiz_admin", adminToken);
      error = "";
      screen = "editor";
      await loadQuestions();
    } catch (err) {
      error = err.message;
      render();
    }
  };
}

async function loadQuestions() {
  const data = await api("/api/admin/questions");
  questions = data.questions || [];
  screen = "editor";
  render();
}

function renderEditor() {
  const items = questions.map((q) => `
    <div class="card">
      <strong>${esc(q.text)}</strong>
      <p class="muted">${q.options.map(esc).join(" · ")} · correct ${esc(q.options[q.correct_index])} · ${q.time_limit}s</p>
      <div class="row">
        <button type="button" data-edit="${q.id}">Edit</button>
        <button type="button" data-del="${q.id}">Delete</button>
      </div>
    </div>`).join("");
  shell("Questions", `
    <form id="qform" class="card stack">
      <label>Question<input name="text" required></label>
      <label>Option A<input name="o0" required></label>
      <label>Option B<input name="o1" required></label>
      <label>Option C<input name="o2"></label>
      <label>Option D<input name="o3"></label>
      <label>Correct option
        <select name="correct">
          <option value="0">A</option><option value="1">B</option>
          <option value="2">C</option><option value="3">D</option>
        </select>
      </label>
      <label>Time limit (seconds)<input name="limit" type="number" min="5" max="120" value="20"></label>
      <button type="submit">Save question</button>
    </form>
    <div class="stack">${items || "<p class='muted'>No questions yet.</p>"}</div>
    <button id="create" type="button">Create game with these questions</button>`);
  document.getElementById("qform").onsubmit = async (event) => {
    event.preventDefault();
    const form = event.target;
    const options = [form.o0.value, form.o1.value, form.o2.value, form.o3.value];
    const body = {
      text: form.text.value,
      options,
      correct_index: Number(form.correct.value),
      time_limit: Number(form.limit.value || 20)
    };
    try {
      const editing = form.dataset.id;
      if (editing) await api("/api/admin/questions/" + editing, { method: "PUT", body: JSON.stringify(body) });
      else await api("/api/admin/questions", { method: "POST", body: JSON.stringify(body) });
      error = "";
      await loadQuestions();
    } catch (err) {
      error = err.message;
      render();
    }
  };
  app.querySelectorAll("[data-del]").forEach((btn) => {
    btn.onclick = async () => {
      await api("/api/admin/questions/" + btn.dataset.del, { method: "DELETE" });
      await loadQuestions();
    };
  });
  app.querySelectorAll("[data-edit]").forEach((btn) => {
    btn.onclick = () => {
      const q = questions.find((item) => String(item.id) === btn.dataset.edit);
      const form = document.getElementById("qform");
      form.dataset.id = q.id;
      form.text.value = q.text;
      form.o0.value = q.options[0] || "";
      form.o1.value = q.options[1] || "";
      form.o2.value = q.options[2] || "";
      form.o3.value = q.options[3] || "";
      form.correct.value = String(q.correct_index);
      form.limit.value = q.time_limit;
    };
  });
  document.getElementById("create").onclick = async () => {
    try {
      const data = await api("/api/admin/games", { method: "POST", body: JSON.stringify({ question_ids: questions.map((q) => q.id) }) });
      pin = data.pin;
      localStorage.setItem("quiz_pin", pin);
      error = "";
      screen = "host";
      state = null;
      connectSocket("admin", adminToken);
      await refresh();
    } catch (err) {
      error = err.message;
      render();
    }
  };
}

function renderJoin() {
  shell("Join", `
    <form id="form" class="card stack">
      <label>Game PIN<input name="pin" inputmode="numeric" maxlength="6" required></label>
      <label>Nickname<input name="nickname" maxlength="16" required></label>
      <button type="submit">Join</button>
    </form>`);
  document.getElementById("form").onsubmit = async (event) => {
    event.preventDefault();
    const form = event.target;
    try {
      const data = await api("/api/join", {
        method: "POST",
        auth: false,
        body: JSON.stringify({ pin: form.pin.value.trim(), nickname: form.nickname.value })
      });
      pin = data.pin;
      playerToken = data.token;
      playerId = data.player_id;
      localStorage.setItem("quiz_pin", pin);
      localStorage.setItem("quiz_player", playerToken);
      localStorage.setItem("quiz_player_id", playerId);
      error = "";
      screen = "play";
      connectSocket("player", playerToken);
      await refresh();
    } catch (err) {
      error = err.message;
      render();
    }
  };
}

function boardHtml() {
  return `<div class="card"><h2>Leaderboard</h2><ol class="board" id="board"></ol></div>`;
}

function renderHost() {
  if (!state) {
    shell("Host", "<p>Connecting…</p>");
    return;
  }
  if (state.phase === "lobby") {
    shell("Lobby", `
      <div class="card">
        <p class="muted">Game PIN</p>
        <div class="pin">${esc(state.pin)}</div>
        <p class="muted" id="count"></p>
        <ul id="players"></ul>
        <div class="row">
          <button id="start" type="button">Start</button>
          <button id="end" type="button">End</button>
        </div>
      </div>
      ${boardHtml()}`);
    document.getElementById("start").onclick = () => postGame("start");
    document.getElementById("end").onclick = () => postGame("end");
  } else if (state.phase === "question") {
    shell("Question", questionCard(true) + boardHtml() + `<div class="row"><button id="next" type="button">Show results</button><button id="end" type="button">End</button></div>`);
    document.getElementById("next").onclick = () => postGame("next");
    document.getElementById("end").onclick = () => postGame("end");
  } else if (state.phase === "reveal") {
    shell("Results", revealCard() + boardHtml() + `<div class="row"><button id="next" type="button">Next</button><button id="end" type="button">End</button></div>`);
    document.getElementById("next").onclick = () => postGame("next");
    document.getElementById("end").onclick = () => postGame("end");
  } else {
    shell("Podium", boardHtml() + `<p><a href="${esc(serverUrl())}/export/${esc(state.pin)}?token=${encodeURIComponent(adminToken)}">Download CSV</a></p>`);
  }
  paintLive();
}

function questionCard(host) {
  const q = state.question;
  const options = q.options.map((text, index) => {
    const picked = !host && state.you && state.you.choice === index;
    return `<button type="button" class="opt-${index}" data-choice="${index}" ${(!host && (sending || (state.you && state.you.answered))) ? "disabled" : ""}>${esc(text)}${picked ? " ✓" : ""}</button>`;
  }).join("");
  return `
    <div class="card stack">
      <p class="muted">Question ${q.index + 1} / ${q.total}</p>
      <h2>${esc(q.text)}</h2>
      <div class="timer" id="timer"></div>
      <div class="bar-track"><div class="bar-fill" id="bar"></div></div>
      <p class="muted" id="count"></p>
      ${host ? "" : `<div class="answers">${options}</div>`}
      ${host ? `<div class="answers">${q.options.map((text, index) => `<div class="card opt-${index}">${esc(text)}</div>`).join("")}</div>` : ""}
    </div>`;
}

function revealCard() {
  const q = state.question;
  const max = Math.max(1, ...(q.distribution || [1]));
  const bars = q.options.map((text, index) => {
    const count = (q.distribution || [])[index] || 0;
    const width = Math.round((count / max) * 100);
    const mark = index === q.correct_index ? "correct" : "";
    return `<div class="${mark}"><div class="line"><div class="fill opt-${index}" style="width:${width}%">${esc(text)} (${count})</div></div></div>`;
  }).join("");
  return `<div class="card stack"><h2>${esc(q.text)}</h2><div class="dist">${bars}</div><p>Correct: ${esc(q.options[q.correct_index])}</p></div>`;
}

async function postGame(action) {
  try {
    const next = await api("/api/admin/games/" + pin + "/" + action, { method: "POST" });
    error = "";
    applyState(next);
  } catch (err) {
    error = err.message;
    render();
  }
}

function renderPlay() {
  if (!state) {
    shell("Game", "<p>Connecting…</p>");
    return;
  }
  const you = state.you || {};
  if (state.phase === "lobby") {
    shell("Lobby", `<div class="card"><h2>You're in, ${esc(you.nickname)}</h2><p>PIN ${esc(state.pin)}</p><p class="muted">Waiting for the host to start.</p><ul id="players"></ul></div>`);
  } else if (state.phase === "question") {
    shell("Question", questionCard(false) + `<p class="muted">Score ${esc(you.score)} · rank #${esc(you.rank)}</p>`);
    app.querySelectorAll("[data-choice]").forEach((btn) => {
      btn.onclick = () => sendAnswer(Number(btn.dataset.choice));
    });
  } else if (state.phase === "reveal") {
    const ok = you.last_correct ? "Correct" : "Not correct";
    shell("Results", revealCard() + `
      <div class="card">
        <h2>${ok}</h2>
        <p>+${esc(you.last_points || 0)} points · total ${esc(you.score)} · rank #${esc(you.rank)}</p>
      </div>` + boardHtml());
  } else {
    shell("Final", `<div class="card"><h2>${esc(you.nickname)} finished #${esc(you.rank)}</h2><p>${esc(you.score)} points</p></div>` + boardHtml());
  }
  paintLive();
}

async function sendAnswer(choice) {
  if (sending) return;
  sending = true;
  render();
  try {
    await api("/api/answer", {
      method: "POST",
      auth: false,
      body: JSON.stringify({ pin, token: playerToken, choice })
    });
    error = "";
    await refresh();
  } catch (err) {
    error = err.message;
  } finally {
    sending = false;
    render();
  }
}

setInterval(() => { refresh().catch(() => {}); }, 1500);
setInterval(paintLive, 200);

(function loadSupabase() {
  if (!cfg.SUPABASE_URL || !cfg.SUPABASE_ANON_KEY) return;
  const script = document.createElement("script");
  script.src = "https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2/dist/umd/supabase.js";
  script.async = true;
  script.onload = () => { if (state && state.game_id) startRealtime(state.game_id); };
  document.head.appendChild(script);
})();

if (screen === "login" && adminToken) {
  screen = "editor";
  loadQuestions().catch(() => { screen = "login"; render(); });
} else {
  render();
}
