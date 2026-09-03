#!/usr/bin/env python3
"""
Free Game Review v2 — chess.com-style analysis on your own machine.

  python review.py game.pgn --engine /path/to/stockfish --depth 18 --json out.json
  python review.py --chesscom USERNAME --engine ... --json out.json      # latest game

Requires: pip install python-chess requests
Optional: openings/*.tsv from https://github.com/lichess-org/chess-openings (for "Book" moves) —
          downloaded automatically on first run if missing and network allows.
"""
import argparse, json, math, os, statistics, sys, io
import chess, chess.engine, chess.pgn

MATE_CP = 10000
HERE = os.path.dirname(os.path.abspath(__file__))
PIECE_VAL = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9, chess.KING: 0}

# ---------- fundamentals ----------
def win_pct(cp: int) -> float:
    return 50 + 50 * (2 / (1 + math.exp(-0.00368208 * max(-MATE_CP, min(MATE_CP, cp)))) - 1)

ACC_K = 0.395   # steepness of the per-move accuracy curve. Lichess uses 0.0435; chess.com's game accuracies fit ~0.395.
def move_accuracy(win_before: float, win_after: float) -> float:
    loss = max(0.0, win_before - win_after)
    return max(0.0, min(100.0, 103.1668 * math.exp(-ACC_K * loss) - 3.1669))

def cp_of(score, color) -> int:
    return score.pov(color).score(mate_score=MATE_CP)

# ---------- thresholds (win% lost) — tunable ----------
# win% lost cutoffs and centipawn lost cutoffs; the harsher of the two wins.
THRESHOLDS    = [("Best", 0.0), ("Excellent", 2.0), ("Good", 7.5), ("Inaccuracy", 12.0), ("Mistake", 20.0), ("Blunder", 1e9)]
CP_THRESHOLDS = [("Best", 0),   ("Excellent", 25),  ("Good", 80),  ("Inaccuracy", 150),  ("Mistake", 400),   ("Blunder", 1e9)]
LOST_CAP = "Mistake"        # when already below 10% you cannot "Blunder"
MISS_MIN_LOSS = 12.0        # missed opportunity must cost at least this much win%
MISS_MATERIAL = 2           # ...or best line wins this much material
COLLAPSE_WIN = 25.0         # if your move leaves you below this win% AND loss>=20 -> Blunder, not Miss
GREAT_GAP = 25.0            # second-best must be this much worse than best
DECIDED = (10.0, 90.0)      # outside this win% band the position is "decided": no Brilliant/Great

SEV = {n: i for i, (n, _) in enumerate(THRESHOLDS)}
def _bucket(val, table):
    for name, cap in table[1:]:
        if val < cap: return name
    return "Blunder"
def base_class(loss: float, is_best: bool, cp_loss: int = 0, w_best: float = 50.0) -> str:
    if is_best or (loss <= 0.0 and cp_loss <= 0):
        return "Best"
    a, b = _bucket(loss, THRESHOLDS), _bucket(cp_loss, CP_THRESHOLDS)
    cls = a if SEV[a] >= SEV[b] else b
    if w_best < 10 and SEV[cls] > SEV[LOST_CAP]: cls = LOST_CAP
    return cls

# ---------- opening book ----------
def load_book():
    book = {}
    d = os.path.join(HERE, "openings")
    files = [os.path.join(d, f + ".tsv") for f in "abcde"]
    if not all(os.path.exists(f) for f in files):
        try:
            import requests
            os.makedirs(d, exist_ok=True)
            for f, p in zip("abcde", files):
                open(p, "w").write(requests.get(f"https://raw.githubusercontent.com/lichess-org/chess-openings/master/{f}.tsv", timeout=20).text)
        except Exception as e:
            print(f"(no opening book: {e})", file=sys.stderr); return book
    for p in files:
        for line in open(p).read().splitlines()[1:]:
            eco, name, pgn = line.split("\t")
            g = chess.pgn.read_game(io.StringIO(pgn)); b = g.board()
            for mv in g.mainline_moves():
                b.push(mv); book.setdefault(b._transposition_key(), None)
            book[b._transposition_key()] = f"{eco} {name}"          # name lives at the END of its line
    return book

# ---------- material / tactics ----------
def material(board, color):
    return sum(PIECE_VAL[p] * len(board.pieces(p, color)) for p in PIECE_VAL)

def pv_material_gain(board, pv, plies=6):
    """Material the side to move gains along the engine PV (net, mover POV)."""
    b = board.copy(); mover = b.turn
    before = material(b, mover) - material(b, not mover)
    for m in pv[:plies]:
        if m not in b.legal_moves: break
        b.push(m)
    return (material(b, mover) - material(b, not mover)) - before

def see(board, square, attacker_color):
    """Static exchange evaluation (swap list): material attacker_color nets by capturing on square."""
    b = board.copy()
    target = b.piece_at(square)
    if not target: return 0
    gain = [PIECE_VAL[target.piece_type]]
    color = attacker_color; d = 0
    while True:
        atts = [s for s in b.attackers(color, square)
                if b.piece_at(s).piece_type != chess.KING or not b.attackers(not color, square)]
        if not atts: break
        s = min(atts, key=lambda s: PIECE_VAL[b.piece_at(s).piece_type])
        d += 1
        gain.append(PIECE_VAL[b.piece_at(s).piece_type] - gain[d - 1])
        b.remove_piece_at(square); b.set_piece_at(square, b.remove_piece_at(s)); color = not color
    if d == 0: return 0
    while d > 1:
        d -= 1
        gain[d - 1] = -max(-gain[d - 1], gain[d])
    return gain[0]

def hangs_piece(board_after, mover):
    """Max material the opponent can win by capturing a mover non-pawn piece (0 if nothing hangs)."""
    opp = not mover; best = 0
    for sq in chess.SquareSet(board_after.occupied_co[mover]):
        p = board_after.piece_at(sq)
        if p.piece_type in (chess.KING, chess.PAWN): continue
        if board_after.attackers(opp, sq):
            best = max(best, see(board_after, sq, opp))
    return best

# ---------- analysis ----------
def analyse_game(game, engine_path, depth, multipv=2, threads=2, book=None, movetime=None, progress_cb=None):
    engine = chess.engine.SimpleEngine.popen_uci(engine_path)
    engine.configure({"Threads": threads, "Hash": 256})
    limit = chess.engine.Limit(time=movetime) if movetime else chess.engine.Limit(depth=depth)
    moves = list(game.mainline_moves())
    positions = []; b = game.board()
    total = len(moves) + 1
    for i in range(total):
        if progress_cb:
            try: progress_cb(i, total)
            except Exception: pass
        positions.append({"board": b.copy(), "info": engine.analyse(b, limit, multipv=multipv)})
        if i < len(moves): b.push(moves[i])
    if progress_cb:
        try: progress_cb(total, total)
        except Exception: pass
    engine.quit()

    results = []; prev_base = None; in_book = True; opening_name = ""
    for i, mv in enumerate(moves):
        pos, nxt = positions[i], positions[i + 1]
        bd = pos["board"]; mover = bd.turn
        top = pos["info"][0]; best_move = top["pv"][0]
        s_best = top["score"].pov(mover); s_played = nxt["info"][0]["score"].pov(mover)
        cp_best, cp_played = s_best.score(mate_score=MATE_CP), s_played.score(mate_score=MATE_CP)
        w_best, w_played = win_pct(cp_best), win_pct(cp_played)
        is_best = (mv == best_move)
        last = bd.peek() if bd.move_stack else None
        is_recapture = last is not None and bd.is_capture(mv) and mv.to_square == last.to_square and positions[i-1]["board"].is_capture(last)
        if is_best: w_played = w_best
        loss = w_best - w_played
        acc = move_accuracy(w_best, w_played)
        second_gap = None
        if len(pos["info"]) > 1:
            second_gap = w_best - win_pct(cp_of(pos["info"][1]["score"], mover))

        cp_loss = 0 if is_best else max(0, max(-1000, min(cp_best, 1000)) - max(-1000, min(cp_played, 1000)))
        cls = base_class(loss, is_best, cp_loss, w_best)
        note = ""

        # --- mate zone: grade by mate distance, not win% ---
        if nxt["board"].is_checkmate():
            cls, acc = "Best", 100.0
        elif s_best.is_mate() and s_best.mate() > 0:                   # mover has forced mate in n_best
            n_best = s_best.mate()
            if s_played.is_mate() and s_played.mate() > 0:
                d = s_played.mate() - (n_best - 1)                     # extra moves added to the mate
                if n_best > 6: cls = "Best" if is_best or d <= 0 else "Excellent"     # long mates: counts are noisy
                else: cls = "Best" if d <= 0 else "Excellent" if d == 1 else "Good" if d == 2 else "Inaccuracy" if d == 3 else ("Miss" if n_best <= 3 else "Mistake")
                if is_best: cls = "Best"
                acc = 100.0 if cls == "Best" else min(acc, 100 - 6 * d)
            elif n_best <= 5:                                          # threw away a short forced mate
                cls, acc = "Miss", min(acc, 40.0)
            # long mate thrown away but still crushing: fall through to win%/cp grading
        elif s_best.is_mate() and s_best.mate() < 0:                   # mover is getting mated whatever
            n_best = -s_best.mate()
            if s_played.is_mate() and s_played.mate() < 0:
                d = n_best - (-s_played.mate())                        # moves we shortened our own demise by
                if n_best > 6: cls = "Best" if is_best or d <= 0 else "Excellent"
                else: cls = "Best" if d <= 0 else "Excellent" if d <= 2 else "Good" if d == 3 else "Inaccuracy" if d <= 5 else "Mistake"
                if is_best: cls = "Best"
            else: cls = "Best"
        else:
            # --- allowed a forced mate when not already lost -> Blunder ---
            if not is_best and s_played.is_mate() and s_played.mate() < 0 and w_best > 10:
                cls, acc = "Blunder", min(acc, 20.0)

        # --- Miss: best line won material/mate, you didn't take it ---
        if cls in ("Inaccuracy", "Mistake", "Blunder") and loss >= MISS_MIN_LOSS and not s_best.is_mate():
            gain = pv_material_gain(bd, top["pv"])
            opp_erred = prev_base in ("Mistake", "Blunder", "Miss")
            if (gain >= MISS_MATERIAL or opp_erred) and not (loss >= 20 and w_played < COLLAPSE_WIN):
                cls = "Miss"; note = f"missed {bd.san(top['pv'][0])}" + (f" winning {gain}" if gain >= MISS_MATERIAL else "")

        # --- Brilliant / Great (only in undecided positions) ---
        if DECIDED[0] < w_best < DECIDED[1]:
            captured = PIECE_VAL[bd.piece_at(mv.to_square).piece_type] if bd.is_capture(mv) and bd.piece_at(mv.to_square) else (1 if bd.is_en_passant(mv) else 0)
            new_hang = hangs_piece(nxt["board"], mover) - hangs_piece(bd, mover)
            if cls in ("Best", "Excellent") and w_played >= 40 and new_hang - captured >= 2:
                cls = "Brilliant"
            elif cls == "Best" and second_gap is not None and second_gap >= GREAT_GAP and not is_recapture:
                cls = "Great"
        # punishing the opponent's error with a clearly-only move is Great even in decided positions
        if cls == "Best" and not is_recapture and prev_base in ("Mistake", "Blunder", "Miss") \
           and second_gap is not None and second_gap >= 10:
            cls = "Great"

        # --- Forced / Book ---
        if bd.legal_moves.count() == 1:
            cls, acc = "Forced", 100.0
        if in_book and book:
            key = nxt["board"]._transposition_key()
            if key in book:
                cls = "Book"; acc = 100.0; opening_name = book[key] or opening_name; note = opening_name
            else: in_book = False

        results.append({
            "ply": i + 1, "move_number": bd.fullmove_number,
            "color": "white" if mover == chess.WHITE else "black",
            "san": bd.san(mv), "best_san": bd.san(best_move), "best_uci": best_move.uci(),
            "eval_before_cp": cp_of(top["score"], chess.WHITE), "eval_after_cp": cp_of(nxt["info"][0]["score"], chess.WHITE),
            "mate_before": s_best.mate() if s_best.is_mate() else None,
            "mate_after": s_played.mate() if s_played.is_mate() else None,
            "win_pct_best": round(w_best, 1), "win_pct_played": round(w_played, 1), "loss": round(loss, 1),
            "second_gap": round(second_gap, 1) if second_gap is not None else None,
            "threat_uci": nxt["info"][0]["pv"][0].uci() if nxt["info"][0].get("pv") else None,
            "threat_san": nxt["board"].san(nxt["info"][0]["pv"][0]) if nxt["info"][0].get("pv") else None,
            "mate_delivered": nxt["board"].is_checkmate(),
            "fen_after": nxt["board"].fen(),
            "classification": cls, "accuracy": round(acc, 1), "note": note, "fen": bd.fen(),
        })
        prev_base = cls if cls in ("Inaccuracy", "Mistake", "Blunder", "Miss") else None
    return results

# ---------- game accuracy ----------
def game_accuracy(results, color, _unused=None):
    """Mean per-move accuracy, book moves excluded (fits chess.com's reported numbers)."""
    accs = [r["accuracy"] for r in results if r["color"] == color and r["classification"] != "Book"]
    return round(sum(accs) / len(accs), 1) if accs else 0.0

# ---------- IO ----------

def list_chesscom_games(user, limit=15):
    import requests, datetime
    h = {"User-Agent": "free-game-review/1.0"}
    r = requests.get(f"https://api.chess.com/pub/player/{user}/games/archives", headers=h, timeout=10)
    if r.status_code != 200:
        return []
    archives = r.json().get("archives", [])
    out = []
    for arch in reversed(archives[-2:]):
        resp = requests.get(arch, headers=h, timeout=10)
        if resp.status_code != 200: continue
        games = resp.json().get("games", [])
        for g in reversed(games):
            url = g.get("url", "")
            gid = url.rstrip("/").split("/")[-1]
            w = g.get("white", {})
            b = g.get("black", {})
            w_user = w.get("username", "")
            b_user = b.get("username", "")
            user_color = "white" if w_user.lower() == user.lower() else "black"
            user_res = w.get("result") if user_color == "white" else b.get("result")
            res_type = "win" if user_res == "win" else ("draw" if user_res in ("agreed", "repetition", "stalemate", "timevsinsufficient", "insufficient") else "loss")
            end_t = g.get("end_time", 0)
            date_str = datetime.datetime.fromtimestamp(end_t).strftime("%b %d, %Y %H:%M") if end_t else ""
            out.append({
                "id": gid,
                "url": url,
                "white": {"username": w_user, "rating": w.get("rating"), "result": w.get("result")},
                "black": {"username": b_user, "rating": b.get("rating"), "result": b.get("result")},
                "time_class": g.get("time_class", ""),
                "time_control": g.get("time_control", ""),
                "date": date_str,
                "user_color": user_color,
                "result": res_type
            })
            if len(out) >= limit:
                return out
    return out

def review_game(game, engine_path, depth=18, threads=2, movetime=1.0, progress_cb=None, username=None):
    book = load_book()
    res = analyse_game(game, engine_path, depth=depth, threads=threads, book=book, movetime=movetime, progress_cb=progress_cb)
    wps = [win_pct(r["eval_after_cp"]) for r in res]
    white = game.headers.get("White", "White")
    black = game.headers.get("Black", "Black")
    flipped = False
    if username:
        flipped = black.lower() == username.strip().lower()
    summary = {
        "white": white,
        "black": black,
        "result": game.headers.get("Result", "*"),
        "depth": depth if not movetime else f"{movetime}s/move",
        "accuracy": {c: game_accuracy(res, c, wps) for c in ("white", "black")},
        "counts": {"white": {}, "black": {}},
        "flipped": flipped
    }
    for r in res:
        d = summary["counts"][r["color"]]
        d[r["classification"]] = d.get(r["classification"], 0) + 1
    return {"summary": summary, "moves": res}

def fetch_chesscom(user, game_id=None):
    import requests
    h = {"User-Agent": "free-game-review/1.0"}
    r = requests.get(f"https://api.chess.com/pub/player/{user}/games/archives", headers=h, timeout=10)
    if r.status_code != 200:
        raise ValueError(f"Chess.com player '{user}' not found.")
    archives = r.json().get("archives", [])
    for url in reversed(archives[-3:]):
        resp = requests.get(url, headers=h, timeout=10)
        if resp.status_code != 200: continue
        for g in reversed(resp.json().get("games", [])):
            if game_id is None or str(game_id) in g.get("url", ""):
                return g["pgn"]
    raise ValueError(f"Game '{game_id or "latest"}' not found for user '{user}'.")

def classify_single_move(board_before: chess.Board, move: chess.Move, engine: chess.engine.SimpleEngine, limit=None, book=None, prev_base=None):
    if limit is None:
        limit = chess.engine.Limit(time=0.35)
    if book is None:
        try: book = load_book()
        except Exception: book = {}

    bd = board_before.copy()
    mover = bd.turn
    try: info_before = engine.analyse(bd, limit, multipv=2)
    except Exception: info_before = engine.analyse(bd, limit, multipv=1)

    top_before = info_before[0] if isinstance(info_before, list) else info_before
    best_move = top_before["pv"][0] if top_before.get("pv") else move

    s_best = top_before["score"].pov(mover)
    cp_best = s_best.score(mate_score=MATE_CP)
    w_best = win_pct(cp_best)

    nxt_bd = bd.copy()
    nxt_bd.push(move)

    try: info_after = engine.analyse(nxt_bd, limit, multipv=1)
    except Exception: info_after = []

    top_after = info_after[0] if isinstance(info_after, list) and info_after else info_after
    if top_after and "score" in top_after:
        s_played = top_after["score"].pov(mover)
        cp_played = s_played.score(mate_score=MATE_CP)
        w_played = win_pct(cp_played)
    else:
        s_played = s_best; cp_played = cp_best; w_played = w_best

    is_best = (move == best_move)
    if is_best: w_played = w_best
    loss = max(0.0, w_best - w_played)
    acc = move_accuracy(w_best, w_played)

    second_gap = None
    if isinstance(info_before, list) and len(info_before) > 1 and "score" in info_before[1]:
        second_gap = w_best - win_pct(cp_of(info_before[1]["score"], mover))

    cp_loss = 0 if is_best else max(0, max(-1000, min(cp_best, 1000)) - max(-1000, min(cp_played, 1000)))
    cls = base_class(loss, is_best, cp_loss, w_best)
    note = ""

    if nxt_bd.is_checkmate():
        cls, acc = "Best", 100.0
    elif s_best.is_mate() and s_best.mate() > 0:
        n_best = s_best.mate()
        if s_played.is_mate() and s_played.mate() > 0:
            d = s_played.mate() - (n_best - 1)
            if n_best > 6: cls = "Best" if is_best or d <= 0 else "Excellent"
            else: cls = "Best" if d <= 0 else "Excellent" if d == 1 else "Good" if d == 2 else "Inaccuracy" if d == 3 else ("Miss" if n_best <= 3 else "Mistake")
            if is_best: cls = "Best"
            acc = 100.0 if cls == "Best" else min(acc, 100 - 6 * d)
        elif n_best <= 5:
            cls, acc = "Miss", min(acc, 40.0)
    elif s_best.is_mate() and s_best.mate() < 0:
        n_best = -s_best.mate()
        if s_played.is_mate() and s_played.mate() < 0:
            d = n_best - (-s_played.mate())
            if n_best > 6: cls = "Best" if is_best or d <= 0 else "Excellent"
            else: cls = "Best" if d <= 0 else "Excellent" if d <= 2 else "Good" if d == 3 else "Inaccuracy" if d <= 5 else "Mistake"
            if is_best: cls = "Best"
        else: cls = "Best"
    else:
        if not is_best and s_played.is_mate() and s_played.mate() < 0 and w_best > 10:
            cls, acc = "Blunder", min(acc, 20.0)

    if cls in ("Inaccuracy", "Mistake", "Blunder") and loss >= MISS_MIN_LOSS and not s_best.is_mate():
        gain = pv_material_gain(bd, top_before.get("pv", []))
        opp_erred = prev_base in ("Mistake", "Blunder", "Miss")
        if (gain >= MISS_MATERIAL or opp_erred) and not (loss >= 20 and w_played < COLLAPSE_WIN):
            cls = "Miss"
            if top_before.get("pv"):
                note = f"missed {bd.san(top_before['pv'][0])}" + (f" winning {gain}" if gain >= MISS_MATERIAL else "")

    if DECIDED[0] < w_best < DECIDED[1]:
        captured = PIECE_VAL[bd.piece_at(move.to_square).piece_type] if bd.is_capture(move) and bd.piece_at(move.to_square) else (1 if bd.is_en_passant(move) else 0)
        new_hang = hangs_piece(nxt_bd, mover) - hangs_piece(bd, mover)
        if cls in ("Best", "Excellent") and w_played >= 40 and new_hang - captured >= 2:
            cls = "Brilliant"
        elif cls == "Best" and second_gap is not None and second_gap >= GREAT_GAP:
            cls = "Great"

    if bd.legal_moves.count() == 1:
        cls, acc = "Forced", 100.0

    if book:
        key = nxt_bd._transposition_key()
        if key in book and book[key]:
            cls = "Book"; acc = 100.0; note = book[key]

    threat_uci = None; threat_san = None
    if top_after and top_after.get("pv"):
        opp_mv = top_after["pv"][0]
        threat_uci = opp_mv.uci()
        threat_san = nxt_bd.san(opp_mv)

    return {
        "san": bd.san(move),
        "uci": move.uci(),
        "color": "white" if mover == chess.WHITE else "black",
        "best_san": bd.san(best_move),
        "best_uci": best_move.uci(),
        "threat_uci": threat_uci,
        "threat_san": threat_san,
        "classification": cls,
        "accuracy": round(acc, 1),
        "eval_before_cp": cp_of(top_before["score"], chess.WHITE),
        "eval_after_cp": cp_of(top_after["score"], chess.WHITE) if top_after else None,
        "mate_before": s_best.mate() if s_best.is_mate() else None,
        "mate_after": s_played.mate() if s_played.is_mate() else None,
        "loss": round(loss, 1),
        "note": note
    }

ICON = {"Brilliant": "!!", "Great": "!", "Best": "★", "Excellent": "✓", "Good": "·", "Book": "B", "Forced": "=",
        "Inaccuracy": "?!", "Mistake": "?", "Blunder": "??", "Miss": "✗"}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pgn", nargs="?"); ap.add_argument("--chesscom"); ap.add_argument("--game-id")
    ap.add_argument("--engine", default=os.environ.get("STOCKFISH", "stockfish"))
    ap.add_argument("--depth", type=int, default=18); ap.add_argument("--threads", type=int, default=max(2, (os.cpu_count() or 2) - 1))
    ap.add_argument("--time", type=float, help="seconds per position instead of fixed depth (e.g. 1.0)")
    ap.add_argument("--json"); a = ap.parse_args()
    if a.chesscom: game = chess.pgn.read_game(io.StringIO(fetch_chesscom(a.chesscom, a.game_id)))
    elif a.pgn: game = chess.pgn.read_game(open(a.pgn))
    else: ap.error("give a PGN file or --chesscom USERNAME")

    res = analyse_game(game, a.engine, a.depth, threads=a.threads, book=load_book(), movetime=a.time)
    wps = [win_pct(r["eval_after_cp"]) for r in res]
    summary = {"white": game.headers.get("White"), "black": game.headers.get("Black"), "result": game.headers.get("Result"),
               "depth": a.depth if not a.time else f"{a.time}s/move", "accuracy": {c: game_accuracy(res, c, wps) for c in ("white", "black")},
               "counts": {"white": {}, "black": {}}}
    for r in res:
        d = summary["counts"][r["color"]]; d[r["classification"]] = d.get(r["classification"], 0) + 1
    print(f"\n{summary['white']} vs {summary['black']}  ({summary['result']})  {summary['depth']}")
    print(f"Accuracy  W {summary['accuracy']['white']}   B {summary['accuracy']['black']}\n")
    for r in res:
        n = f"{r['move_number']}." if r["color"] == "white" else "   "
        best = "" if r["classification"] in ("Best", "Brilliant", "Great", "Book", "Forced") else f"  best {r['best_san']}"
        ev = f"M{r['mate_after']}" if r["mate_after"] else f"{r['eval_after_cp']/100:+.2f}"
        print(f"{n:>5} {r['san']:<8} {ICON[r['classification']]:>2} {r['classification']:<10} eval {ev:>6}  acc {r['accuracy']:5.1f}{best}  {r['note']}")
    print("\n" + json.dumps(summary["counts"], indent=1))
    if a.json:
        json.dump({"summary": summary, "moves": res}, open(a.json, "w"), indent=1); print(f"\nwrote {a.json}")

if __name__ == "__main__":
    main()
