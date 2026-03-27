from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit
import sqlite3
import random
import os

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "pokerkey123")
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# ===== DATABASE =====
conn = sqlite3.connect("db.db", check_same_thread=False)
c = conn.cursor()
c.execute("""
CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY,
    chips INTEGER
)
""")
conn.commit()

def get_chips(user):
    c.execute("SELECT chips FROM users WHERE username=?", (user,))
    r = c.fetchone()
    if r:
        return r[0]
    c.execute("INSERT INTO users VALUES (?,?)", (user, 1000))
    conn.commit()
    return 1000

def set_chips(user, amount):
    get_chips(user)
    c.execute("UPDATE users SET chips=? WHERE username=?", (amount, user))
    conn.commit()

# ===== DECK =====
def create_deck():
    suits = ["♠","♥","♦","♣"]
    ranks = ["2","3","4","5","6","7","8","9","10","J","Q","K","A"]
    d = [r+s for s in suits for r in ranks]
    random.shuffle(d)
    return d

# ===== HAND EVALUATION =====
RANK_MAP = {"2":2,"3":3,"4":4,"5":5,"6":6,"7":7,"8":8,"9":9,"10":10,"J":11,"Q":12,"K":13,"A":14}

def card_rank(card):
    return RANK_MAP.get(card[:-1], 0)

def card_suit(card):
    return card[-1]

def best_hand(hole, community):
    from itertools import combinations
    all_cards = hole + community
    best = None
    for combo in combinations(all_cards, 5):
        score = evaluate_5(list(combo))
        if best is None or score > best:
            best = score
    return best

def evaluate_5(cards):
    ranks = sorted([card_rank(c) for c in cards], reverse=True)
    suits = [card_suit(c) for c in cards]
    flush = len(set(suits)) == 1
    straight = (ranks == list(range(ranks[0], ranks[0]-5, -1))) or (sorted(ranks) == [2,3,4,5,14])
    from collections import Counter
    cnt = Counter(ranks)
    counts = sorted(cnt.values(), reverse=True)
    unique = sorted(cnt.keys(), key=lambda r: (cnt[r], r), reverse=True)
    if straight and flush:
        return (8, ranks)
    if counts[0] == 4:
        return (7, unique)
    if counts[:2] == [3, 2]:
        return (6, unique)
    if flush:
        return (5, ranks)
    if straight:
        return (4, ranks)
    if counts[0] == 3:
        return (3, unique)
    if counts[:2] == [2, 2]:
        return (2, unique)
    if counts[0] == 2:
        return (1, unique)
    return (0, ranks)

# ===== GAME STATE =====
game = {
    "players": [],
    "spectators": [],
    "hands": {},
    "community": [],
    "deck": [],
    "pot": 0,
    "stage": "waiting",
    "current_turn": 0,
    "current_bet": 0,
    "dealer": 0,
    "small_blind": 50,
    "big_blind": 100,
    "round_bets": {},
    "acted": set(),
    "last_raiser": None,
}

def broadcast_state():
    state = {
        "players": [
            {
                "name": p["name"],
                "chips": p["chips"],
                "bet": p["bet"],
                "folded": p["folded"],
                "allin": p["allin"],
                "is_turn": (i == game["current_turn"] and game["stage"] not in ["waiting","showdown"])
            }
            for i, p in enumerate(game["players"])
        ],
        "community": game["community"],
        "pot": game["pot"],
        "stage": game["stage"],
        "current_bet": game["current_bet"],
        "spectator_count": len(game["spectators"]),
    }
    for p in game["players"]:
        if p["sid"] and p["name"] in game["hands"]:
            personal_state = dict(state)
            personal_state["my_hand"] = game["hands"][p["name"]]
            personal_state["my_name"] = p["name"]
            socketio.emit("state", personal_state, room=p["sid"])
    for sid in game["spectators"]:
        spec_state = dict(state)
        spec_state["my_hand"] = []
        spec_state["my_name"] = "spectator"
        socketio.emit("state", spec_state, room=sid)

def next_turn():
    active = [i for i, p in enumerate(game["players"]) if not p["folded"] and not p["allin"]]
    if not active:
        end_round()
        return
    idx = game["current_turn"]
    for _ in range(len(game["players"])):
        idx = (idx + 1) % len(game["players"])
        p = game["players"][idx]
        if not p["folded"] and not p["allin"]:
            game["current_turn"] = idx
            broadcast_state()
            return
    end_round()

def check_round_over():
    active = [p for p in game["players"] if not p["folded"] and not p["allin"]]
    not_folded = [p for p in game["players"] if not p["folded"]]
    if len(not_folded) == 1:
        end_round()
        return True
    all_matched = all(p["bet"] == game["current_bet"] or p["allin"] for p in active)
    all_acted = all(p["name"] in game["acted"] for p in active)
    if all_matched and all_acted:
        advance_stage()
        return True
    return False

def advance_stage():
    game["acted"] = set()
    game["current_bet"] = 0
    for p in game["players"]:
        p["bet"] = 0
    if game["stage"] == "preflop":
        game["community"] = [game["deck"].pop(), game["deck"].pop(), game["deck"].pop()]
        game["stage"] = "flop"
    elif game["stage"] == "flop":
        game["community"].append(game["deck"].pop())
        game["stage"] = "turn"
    elif game["stage"] == "turn":
        game["community"].append(game["deck"].pop())
        game["stage"] = "river"
    elif game["stage"] == "river":
        end_round()
        return
    active_indices = [i for i, p in enumerate(game["players"]) if not p["folded"] and not p["allin"]]
    if active_indices:
        dealer = game["dealer"]
        for i in range(1, len(game["players"])+1):
            idx = (dealer + i) % len(game["players"])
            if idx in active_indices:
                game["current_turn"] = idx
                break
    broadcast_state()

def end_round():
    not_folded = [p for p in game["players"] if not p["folded"]]
    if len(not_folded) == 1:
        winner = not_folded[0]
        winner["chips"] += game["pot"]
        set_chips(winner["name"], winner["chips"])
        result = {"winner": winner["name"], "reason": "everyone folded", "pot": game["pot"], "hands": {}}
    else:
        scores = {}
        for p in not_folded:
            hand = game["hands"].get(p["name"], [])
            scores[p["name"]] = best_hand(hand, game["community"])
        winner_name = max(scores, key=lambda n: scores[n])
        winner = next(p for p in game["players"] if p["name"] == winner_name)
        winner["chips"] += game["pot"]
        set_chips(winner["name"], winner["chips"])
        result = {
            "winner": winner_name,
            "reason": "best hand",
            "pot": game["pot"],
            "hands": {p["name"]: game["hands"].get(p["name"], []) for p in not_folded}
        }
    game["stage"] = "showdown"
    game["pot"] = 0
    for p in game["players"]:
        p["chips"] = get_chips(p["name"])
        p["bet"] = 0
        p["folded"] = False
        p["allin"] = False
    broadcast_state()
    socketio.emit("showdown", result)

@app.route("/")
def index():
    return render_template("index.html")

@socketio.on("join")
def on_join(data):
    username = data.get("username", "").strip()
    if not username:
        return
    sid = request.sid
    existing = next((p for p in game["players"] if p["name"] == username), None)
    if existing:
        existing["sid"] = sid
        emit("joined", {"role": "player", "chips": existing["chips"]})
        broadcast_state()
        return
    if game["stage"] == "waiting" and len(game["players"]) < 4:
        chips = get_chips(username)
        game["players"].append({"name": username, "chips": chips, "bet": 0, "folded": False, "allin": False, "sid": sid})
        emit("joined", {"role": "player", "chips": chips})
    else:
        if sid not in game["spectators"]:
            game["spectators"].append(sid)
        emit("joined", {"role": "spectator", "chips": 0})
    broadcast_state()

@socketio.on("start_game")
def on_start():
    if len(game["players"]) < 2:
        emit("error", {"msg": "Minimal 2 pemain!"})
        return
    if game["stage"] != "waiting":
        emit("error", {"msg": "Game sudah berjalan!"})
        return
    game["deck"] = create_deck()
    game["community"] = []
    game["pot"] = 0
    game["stage"] = "preflop"
    game["acted"] = set()
    game["current_bet"] = game["big_blind"]
    for p in game["players"]:
        p["folded"] = False
        p["allin"] = False
        p["bet"] = 0
    game["hands"] = {}
    for p in game["players"]:
        game["hands"][p["name"]] = [game["deck"].pop(), game["deck"].pop()]
    n = len(game["players"])
    dealer = game["dealer"] % n
    sb = (dealer + 1) % n
    bb = (dealer + 2) % n
    utg = (dealer + 3) % n if n > 2 else (dealer + 1) % n
    def post_blind(idx, amount):
        p = game["players"][idx]
        actual = min(amount, p["chips"])
        p["chips"] -= actual
        p["bet"] = actual
        game["pot"] += actual
        if p["chips"] == 0:
            p["allin"] = True
        set_chips(p["name"], p["chips"])
    post_blind(sb, game["small_blind"])
    post_blind(bb, game["big_blind"])
    game["current_turn"] = utg % n
    game["last_raiser"] = bb
    broadcast_state()

@socketio.on("action")
def on_action(data):
    sid = request.sid
    action = data.get("action")
    amount = int(data.get("amount", 0))
    player = next((p for p in game["players"] if p["sid"] == sid), None)
    if not player:
        return
    idx = game["players"].index(player)
    if idx != game["current_turn"]:
        emit("error", {"msg": "Bukan giliran kamu!"})
        return
    if game["stage"] in ["waiting", "showdown"]:
        return
    if action == "fold":
        player["folded"] = True
        game["acted"].add(player["name"])
    elif action == "check":
        if game["current_bet"] > player["bet"]:
            emit("error", {"msg": "Tidak bisa check, ada bet!"})
            return
        game["acted"].add(player["name"])
    elif action == "call":
        to_call = game["current_bet"] - player["bet"]
        actual = min(to_call, player["chips"])
        player["chips"] -= actual
        player["bet"] += actual
        game["pot"] += actual
        if player["chips"] == 0:
            player["allin"] = True
        set_chips(player["name"], player["chips"])
        game["acted"].add(player["name"])
    elif action == "raise":
        total_bet = game["current_bet"] + max(amount, game["big_blind"])
        to_add = total_bet - player["bet"]
        actual = min(to_add, player["chips"])
        player["chips"] -= actual
        player["bet"] += actual
        game["pot"] += actual
        game["current_bet"] = player["bet"]
        if player["chips"] == 0:
            player["allin"] = True
        set_chips(player["name"], player["chips"])
        game["acted"] = {player["name"]}
        game["last_raiser"] = idx
    elif action == "allin":
        actual = player["chips"]
        player["bet"] += actual
        game["pot"] += actual
        player["chips"] = 0
        player["allin"] = True
        if player["bet"] > game["current_bet"]:
            game["current_bet"] = player["bet"]
            game["acted"] = {player["name"]}
        else:
            game["acted"].add(player["name"])
        set_chips(player["name"], 0)
    if not check_round_over():
        next_turn()

@socketio.on("new_round")
def on_new_round():
    if game["stage"] != "showdown":
        return
    game["players"] = [p for p in game["players"] if p["chips"] > 0]
    if len(game["players"]) < 2:
        game["stage"] = "waiting"
        broadcast_state()
        return
    game["dealer"] = (game["dealer"] + 1) % len(game["players"])
    game["stage"] = "waiting"
    broadcast_state()

@socketio.on("disconnect")
def on_disconnect():
    sid = request.sid
    game["spectators"] = [s for s in game["spectators"] if s != sid]
    for p in game["players"]:
        if p["sid"] == sid:
            p["sid"] = None
    broadcast_state()

@socketio.on("reset_game")
def on_reset():
    game["players"] = []
    game["spectators"] = []
    game["hands"] = {}
    game["community"] = []
    game["deck"] = []
    game["pot"] = 0
    game["stage"] = "waiting"
    game["current_turn"] = 0
    game["current_bet"] = 0
    game["dealer"] = 0
    game["acted"] = set()
    game["last_raiser"] = None
    broadcast_state()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port, debug=False)
