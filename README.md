# Free Game Review

chess.com-style Game Review, run locally. Stockfish + python-chess, no subscription.

## Install (once)
```
pip3 install python-chess requests
brew install stockfish            # macOS. Linux/Windows: stockfishchess.org/download
```

## Use

### Web UI (Recommended)
Launch the server and pick games directly in your browser:
```
python3 gr.py
```
Opens `http://localhost:8000` automatically. Everything is built into the UI:
- **Choose Username:** Enter any Chess.com username to view recent games with opponent ratings, time controls, dates, and win/loss indicators.
- **Select Game or Game ID:** Click any recent game to review it, or enter a specific Game ID / Chess.com URL directly.
- **Live Progress:** Watch real-time Stockfish progress as moves are analyzed.
- **Change Games Anytime:** Click **♟ Choose Game** in the top header at any point to load another game without touching the terminal.

### Command Line (Optional)
```
python3 gr.py exhaustknight                  # your latest chess.com game
python3 gr.py exhaustknight 173871734098     # a specific game (id is in the game URL)
python3 gr.py tests/game2.pgn                # any PGN file
```
(`chmod +x gr.py` once if you'd rather type `./gr.py`.)

Options: `--time 2` (slower, more accurate) · `--engine /path/to/stockfish` · `--port 8080` · `--no-open`

## What you get
Board with move highlights and classification badges, arrows, animated moves, sound, eval bar and
game arc, move list, per-move coach text, accuracy per side, and the counts table.

**Arrows.** Two, following chess.com's scheme:
- **green** — the engine's best move. Shown on Excellent, Good, Inaccuracy, Mistake, Miss, and Blunder (suppressed on Best/Brilliant/Great/Book/Forced where it duplicates the played move, and shown from the current position in free-play analysis mode).
- **red** — the threat you have just handed the opponent: their strongest reply in the position
  *after* your move. Appears on Mistake, Miss and Blunder only.

Knight moves bend; the arrowhead sits under the piece. The **Best** button can toggle arrows on or off.
(chess.com's own suggestion arrow is blue by default and user-recolourable, which is why its colour
varies between accounts and screenshots — the green/red pair above is the default scheme.)

**Move visuals.** Both squares of the played move are tinted in its classification colour, and a
badge sits on the destination: `!!` Brilliant, `!` Great, ★ Best, thumbs-up Excellent, check Good,
book Book, arrow Forced, `?!` Inaccuracy, `?` Mistake, ✕ Miss, `??` Blunder. On checkmate the mating
move keeps its badge and a crown appears on the winner's king, with the result on the eval bar.

**Play your own moves.** Click a piece (or drag it) anywhere in the review and play whatever you
like. The board switches to Analysis mode: the engine evaluates each new position, the eval bar and
best-move arrow follow your line, and the line is listed at the top. **Back to game** returns you to
the move you left. This needs the server running, which it is whenever the page is open.

**Sound** uses clean open-source sound effects (standard Lichess sound set): move, capture, check, castle, promotion,
game end, victory, brilliant fanfare, and illegal move buzz. Instant 0ms playback
via Web Audio API and preloaded audio buffers. Toggle with the speaker in the bottom-right.

Classifications: Brilliant, Great, Best, Excellent, Good, Book, Forced, Inaccuracy, Mistake, Miss, Blunder.

## Accuracy vs chess.com
Calibrated against two reviewed games: labels agree ~80% of the time, game accuracy within ~2 points.
The rest is engine-depth noise on near-equal moves.

To improve it: get a game chess.com has reviewed, save its labels one-per-ply space-separated to
`labels.txt`, then `python3 tests/calibrate.py ui/out.json labels.txt` (or compare calibration sets: `python3 tests/calibrate.py tests/real_out.json tests/chesscom_labels.txt`). It prints a confusion matrix and the win%-loss range per label. Tune `THRESHOLDS` / `ACC_K` at the top of review.py.

## Repository Structure
- `gr.py` — launcher and local analysis server
- `review.py` — Stockfish engine analysis, move classification, and Chess.com API integration
- `ui/` — interactive web interface (`index.html`, `assets/`, `out.json`)
- `openings/` — Lichess opening book ECO TSVs
- `tests/` — sample games (`*.pgn`), calibration label datasets (`*.txt`, `*.json`), and `calibrate.py`
- `tools/` — developer and scraping utilities (`tools/harvest/`)
- `docs/` — reference screenshots (`docs/screenshots/`)

The server also exposes `/api/legal`, `/api/move` and `/api/analyze` on localhost, which is what
makes free play work. Re-open the last analysed game without re-running the engine:
`python3 gr.py --skip-analysis`.

Clean and publishable: piece vectors use Colin M. Burnett's standard chess pieces (CC BY-SA 3.0), sounds use the open-source Lichess sound set, and typography uses crisp system fonts.
