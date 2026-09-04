#!/usr/bin/env python3
"""
Game Review — one command.

  python3 gr.py game.pgn                       analyse a PGN and open the review
  python3 gr.py exhaustknight                  that chess.com user's latest game
  python3 gr.py exhaustknight 173871734098     a specific chess.com game id
  python3 gr.py "<chess.com game url>"         paste a URL (must contain ?username=)

Options: --time 1 (seconds/move) --engine PATH --host 127.0.0.1 --port 8000 --no-open --skip-analysis

While the page is open the same server also answers:
  GET  /api/legal?fen=...            legal moves for a position
  POST /api/move    {fen, uci}       make a move -> new fen, san, flags
  POST /api/analyze {fen, time}      eval, best move, principal variation
so you can play your own moves on the board and keep getting analysis.
"""
import argparse, atexit, http.server, io, json, os, re, shutil, socketserver, subprocess, sys, threading, urllib.parse, webbrowser
import review

HERE = os.path.dirname(os.path.abspath(__file__))
UI = os.path.join(HERE, "ui")
POOL = None
ENGINE_PATH = None
REVIEW_LOCK = threading.Lock()

MAX_BODY_BYTES = 1 << 20        # 1 MB: nothing we accept is legitimately larger
MAX_MOVETIME = 5.0              # seconds per position, per request
MAX_PLIES = 600                 # refuse absurd PGNs rather than tie up the engine for hours

REVIEW_STATUS = {
    "status": "idle",
    "current": 0,
    "total": 0,
    "percent": 0,
    "message": "",
    "error": None,
    "data": None
}

def _clamp_time(raw, default, lo, hi):
    try: t = float(raw)
    except (TypeError, ValueError): t = default
    if t != t: t = default          # NaN
    return max(lo, min(hi, t))

class Engines:
    """One shared engine protected by a lock to avoid leaking processes per request."""
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.e = None

    def _ensure(self):
        if self.e is None:
            import chess.engine
            self.e = chess.engine.SimpleEngine.popen_uci(self.path)
            threads = max(2, (os.cpu_count() or 4) - 1)
            self.e.configure({"Threads": threads, "Hash": 256})
        return self.e

    def _reset(self):
        """Drop a dead engine. Caller must already hold self.lock."""
        if self.e is not None:
            try: self.e.quit()
            except Exception: pass
            self.e = None

    def _run(self, fn):
        with self.lock:
            try:
                return fn(self._ensure())
            except Exception:
                # a crashed Stockfish used to wedge every later request forever
                self._reset()
                return fn(self._ensure())

    def analyse(self, board, limit, **kw):
        return self._run(lambda e: e.analyse(board, limit, **kw))

    def classify(self, b_before, move, limit=None, prev_base=None):
        return self._run(lambda e: review.classify_single_move(
            b_before, move, e, limit=limit, book=review.load_book(), prev_base=prev_base))

    def close(self):
        with self.lock:
            self._reset()

class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw): super().__init__(*a, directory=UI, **kw)
    def log_message(self, *a): pass

    def _send(self, obj, code=200):
        b = json.dumps(obj).encode()
        try:
            self.send_response(code); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
        except (BrokenPipeError, ConnectionResetError):
            pass                    # client moved on (superseded analysis) — not an error

    def do_GET(self):
        if self.path.startswith("/api/legal"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            return self._legal(q.get("fen", [""])[0])
        if self.path.startswith("/api/games"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            username = q.get("username", [""])[0].strip()
            if not username:
                return self._send({"error": "username required"}, 400)
            try:
                games = review.list_chesscom_games(username, limit=15)
                return self._send({"games": games})
            except Exception as e:
                return self._send({"error": str(e)}, 500)
        if self.path.startswith("/api/review-status"):
            with REVIEW_LOCK:
                return self._send(REVIEW_STATUS)
        return super().do_GET()

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            return self._send({"error": "bad content-length"}, 400)
        if n < 0:
            return self._send({"error": "bad content-length"}, 400)
        if n > MAX_BODY_BYTES:
            return self._send({"error": "request body too large"}, 413)
        try: body = json.loads(self.rfile.read(n) or b"{}")
        except Exception: return self._send({"error": "bad json"}, 400)
        if not isinstance(body, dict): return self._send({"error": "bad json"}, 400)
        if self.path.startswith("/api/move"): return self._move(body)
        if self.path.startswith("/api/classify"): return self._classify(body)
        if self.path.startswith("/api/analyze"): return self._analyze(body)
        if self.path.startswith("/api/review"): return self._start_review(body)
        return self._send({"error": "not found"}, 404)

    def _legal(self, fen):
        import chess
        try: b = chess.Board(fen)
        except Exception: return self._send({"error": "bad fen"}, 400)
        moves = {}
        for m in b.legal_moves:
            moves.setdefault(chess.square_name(m.from_square), []).append(chess.square_name(m.to_square))
        self._send({"moves": moves, "turn": "w" if b.turn else "b",
                    "check": b.is_check(), "over": b.is_game_over()})

    def _classify(self, body):
        import chess
        try:
            fen_before = body.get("fen_before")
            uci = body.get("uci")
            b_before = chess.Board(fen_before)
            try: m = chess.Move.from_uci(uci)
            except Exception: m = None
            if not m or m not in b_before.legal_moves:
                try: m = chess.Move.from_uci(uci + "q")
                except Exception: m = None
            if not m or m not in b_before.legal_moves:
                return self._send({"error": "illegal move"}, 400)
        except Exception as e:
            return self._send({"error": f"invalid input: {str(e)}"}, 400)

        import chess.engine
        t = _clamp_time(body.get("time", 0.8), 0.8, 0.15, 2.0)
        limit = chess.engine.Limit(time=t)
        prev_base = body.get("prev_base")

        try:
            res = POOL.classify(b_before, m, limit=limit, prev_base=prev_base)
            self._send(res)
        except Exception as e:
            self._send({"error": str(e)}, 500)

    def _move(self, body):
        import chess
        try: b = chess.Board(body["fen"])
        except Exception: return self._send({"error": "bad fen"}, 400)
        try: m = chess.Move.from_uci(body.get("uci", ""))
        except Exception: return self._send({"error": "bad move"}, 400)
        if m not in b.legal_moves:
            try: m = chess.Move.from_uci(body["uci"] + "q")          # auto-queen
            except Exception: return self._send({"error": "illegal"}, 400)
            if m not in b.legal_moves: return self._send({"error": "illegal"}, 400)
        san = b.san(m); cap = b.is_capture(m) or b.is_en_passant(m); castle = b.is_castling(m)
        b.push(m)
        self._send({"fen": b.fen(), "san": san, "uci": m.uci(), "capture": cap, "castle": castle,
                    "promotion": m.promotion is not None, "check": b.is_check(),
                    "mate": b.is_checkmate(), "over": b.is_game_over(),
                    "turn": "w" if b.turn else "b"})

    def _analyze(self, body):
        import chess, chess.engine
        try: b = chess.Board(body["fen"])
        except Exception: return self._send({"error": "bad fen"}, 400)
        if b.is_game_over():
            return self._send({"cp": 0, "mate": None, "best_uci": None, "best_san": None, "over": True})
        t = _clamp_time(body.get("time", 0.8), 0.8, 0.1, MAX_MOVETIME)
        try: info = POOL.analyse(b, chess.engine.Limit(time=t), multipv=1)
        except Exception as e: return self._send({"error": str(e)}, 500)
        top = info[0] if isinstance(info, list) else info
        sc = top["score"].pov(chess.WHITE); pv = top.get("pv", [])
        self._send({"cp": sc.score(mate_score=10000), "mate": sc.mate() if sc.is_mate() else None,
                    "best_uci": pv[0].uci() if pv else None, "best_san": b.san(pv[0]) if pv else None,
                    "pv": [x.uci() for x in pv[:6]], "over": False})

    def _start_review(self, body):
        with REVIEW_LOCK:
            if REVIEW_STATUS.get("status") == "running":
                return self._send({"error": "Analysis is already in progress"}, 409)
            REVIEW_STATUS.update({
                "status": "running",
                "current": 0,
                "total": 0,
                "percent": 0,
                "message": "Fetching game data...",
                "error": None,
                "data": None
            })
        t = threading.Thread(target=self._run_async_review, args=(body,), daemon=True)
        t.start()
        return self._send({"status": "started"})

    def _run_async_review(self, body):
        """Wrapper that guarantees the status never stays 'running' after the thread ends.
        It previously could: SystemExit/MemoryError are not Exception, so a death here left
        every later review returning 409 until the process was restarted."""
        try:
            self._do_review(body)
        except Exception as e:
            with REVIEW_LOCK:
                REVIEW_STATUS["status"] = "error"
                REVIEW_STATUS["error"] = str(e)
                REVIEW_STATUS["message"] = f"Error: {str(e)}"
        finally:
            with REVIEW_LOCK:
                if REVIEW_STATUS.get("status") == "running":
                    REVIEW_STATUS["status"] = "error"
                    REVIEW_STATUS["error"] = REVIEW_STATUS.get("error") or "analysis stopped unexpectedly"
                    REVIEW_STATUS["message"] = f"Error: {REVIEW_STATUS['error']}"

    def _do_review(self, body):
        import chess.pgn
        username = body.get("username", "").strip() or None
        game_id = body.get("game_id", "").strip() or None
        url = body.get("url", "").strip()
        pgn = body.get("pgn", "").strip()
        movetime = _clamp_time(body.get("time", 1.0), 1.0, 0.05, MAX_MOVETIME)

        if url:
            m = re.search(r"chess\.com/.*?/(\d{6,})", url)
            if m: game_id = m.group(1)
            u = re.search(r"username=([\w-]+)", url)
            if u and not username: username = u.group(1)

        if pgn:
            pgn_text = pgn
        elif username or game_id:
            with REVIEW_LOCK:
                msg = f"Fetching game from Chess.com for {username}..." if username else f"Fetching game {game_id} from Chess.com..."
                REVIEW_STATUS["message"] = msg
            pgn_text = review.fetch_chesscom(username, game_id)
        else:
            raise ValueError("Please provide a username, Game ID, Chess.com URL, or PGN.")

        game = chess.pgn.read_game(io.StringIO(pgn_text))
        if not game:
            raise ValueError("Could not parse chess game from PGN.")
        n_plies = sum(1 for _ in game.mainline_moves())
        if n_plies == 0:
            raise ValueError("That game has no moves to analyse.")
        if n_plies > MAX_PLIES:
            raise ValueError(f"Game is too long to analyse ({n_plies} plies, limit {MAX_PLIES}).")

        def on_progress(curr, tot):
            with REVIEW_LOCK:
                REVIEW_STATUS["current"] = curr
                REVIEW_STATUS["total"] = tot
                pct = round(curr / tot * 100) if tot else 0
                REVIEW_STATUS["percent"] = pct
                REVIEW_STATUS["message"] = f"Analyzing move {curr} of {tot} ({pct}%)..."

        with REVIEW_LOCK:
            REVIEW_STATUS["message"] = "Initializing Stockfish engine..."

        engine_path = ENGINE_PATH or find_engine(None)
        res_data = review.review_game(
            game, engine_path, threads=2, movetime=movetime,
            progress_cb=on_progress, username=username
        )

        out_file = os.path.join(UI, "out.json")
        tmp_file = out_file + ".tmp"
        with open(tmp_file, "w") as f:
            json.dump(res_data, f, indent=1)
        os.replace(tmp_file, out_file)

        with REVIEW_LOCK:
            REVIEW_STATUS["status"] = "done"
            REVIEW_STATUS["current"] = REVIEW_STATUS["total"]
            REVIEW_STATUS["percent"] = 100
            REVIEW_STATUS["message"] = "Analysis complete!"
            REVIEW_STATUS["data"] = res_data

class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True; daemon_threads = True

def find_engine(explicit):
    for c in [explicit, os.environ.get("STOCKFISH"), "stockfish"]:
        if not c: continue
        if os.path.sep in c and os.path.exists(c): return os.path.abspath(c)
        w = shutil.which(c)
        if w: return w
    # raise rather than sys.exit: this is also called from the review worker thread,
    # where SystemExit would bypass the error handler and leave the status stuck.
    raise RuntimeError("Stockfish not found. Install it (macOS: brew install stockfish) "
                       "or pass --engine /path/to/stockfish")

def run_review(engine, target, game_id, movetime, out):
    cmd = [sys.executable, os.path.join(HERE, "review.py"), "--engine", engine,
           "--time", str(movetime), "--json", out]
    m = re.search(r"chess\.com/.*?/(\d{6,})", target)
    if m:
        u = re.search(r"username=([\w-]+)", target)
        if not u: sys.exit("URL has no ?username= — use: python3 gr.py USERNAME GAMEID")
        cmd += ["--chesscom", u.group(1), "--game-id", m.group(1)]
    elif os.path.exists(target): cmd.append(target)
    else: cmd += ["--chesscom", target] + (["--game-id", game_id] if game_id else [])
    print("analysing…  (about %gs per move)" % movetime)
    if subprocess.run(cmd).returncode != 0: sys.exit(1)
    if "--chesscom" in cmd:
        u = cmd[cmd.index("--chesscom") + 1].lower()
        d = json.load(open(out))
        d["summary"]["flipped"] = d["summary"].get("black", "").lower() == u
        json.dump(d, open(out, "w"))

def main():
    global POOL, ENGINE_PATH
    ap = argparse.ArgumentParser(usage=__doc__)
    ap.add_argument("target", nargs="?"); ap.add_argument("game_id", nargs="?")
    ap.add_argument("--engine"); ap.add_argument("--time", type=float, default=1.0)
    ap.add_argument("--host", default="127.0.0.1", help="host address to bind (default: 127.0.0.1)")
    ap.add_argument("--port", type=int, default=8000); ap.add_argument("--no-open", action="store_true")
    ap.add_argument("--skip-analysis", action="store_true", help="serve the last result without re-analysing")
    a = ap.parse_args()

    if not os.path.isdir(UI): sys.exit(f"missing {UI}")
    try:
        ENGINE_PATH = find_engine(a.engine)
    except RuntimeError as e:
        sys.exit(str(e))
    POOL = Engines(ENGINE_PATH)
    atexit.register(lambda: POOL.close() if POOL else None)
    out = os.path.join(UI, "out.json")
    if a.target and not a.skip_analysis: run_review(ENGINE_PATH, a.target, a.game_id, a.time, out)

    if a.host not in ("127.0.0.1", "localhost"):
        print(f"WARNING: Binding to non-loopback interface '{a.host}'. Review APIs are unauthenticated; place behind a trusted reverse proxy.")

    with Server((a.host, a.port), Handler) as httpd:
        host_display = "localhost" if a.host in ("127.0.0.1", "0.0.0.0") else a.host
        url = f"http://{host_display}:{a.port}"
        print(f"\nReview ready → {url}    (ctrl-C to stop)")
        if not a.no_open: threading.Timer(0.6, lambda: webbrowser.open(url)).start()
        try: httpd.serve_forever()
        except KeyboardInterrupt: print("\nstopped")
        finally:
            if POOL: POOL.close()

if __name__ == "__main__":
    main()
