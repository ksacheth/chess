#!/usr/bin/env python3
"""
Calibrate classification thresholds against chess.com.

  python tests/calibrate.py tests/real_out.json tests/chesscom_labels.txt

chesscom_labels.txt: one label per ply, in order, as chess.com shows them, e.g.
  Book Book Best Excellent Inaccuracy Best Blunder Great ...
(whitespace or newline separated; case-insensitive; "Book" allowed)

Prints: per-ply diff, confusion matrix, agreement %, and the win%-loss distribution
per chess.com label so you can see where their cutoffs actually sit.
"""
import json, sys, collections

ORDER = ["Brilliant","Great","Best","Excellent","Good","Book","Inaccuracy","Mistake","Miss","Blunder"]

def main(json_path, labels_path):
    d = json.load(open(json_path)); moves = d["moves"]
    theirs = [t.capitalize() for t in open(labels_path).read().split()]
    n = min(len(moves), len(theirs))
    if len(moves) != len(theirs):
        print(f"WARNING: {len(moves)} plies analysed vs {len(theirs)} labels given; comparing first {n}")
    conf = collections.Counter(); loss_by_label = collections.defaultdict(list); agree = 0
    print(f"{'ply':>4} {'move':<7} {'mine':<11} {'chess.com':<11} {'loss':>6}")
    for i in range(n):
        m, t = moves[i], theirs[i]
        mine = m["classification"]
        if mine == t: agree += 1
        else: print(f"{i+1:>4} {m['san']:<7} {mine:<11} {t:<11} {m['loss']:>6.1f}")
        conf[(t, mine)] += 1
        if t != "Book": loss_by_label[t].append(m["loss"])
    print(f"\nagreement: {agree}/{n} = {100*agree/n:.0f}%")

    print("\nconfusion (rows = chess.com, cols = mine)")
    cols = [c for c in ORDER if any(conf[(r, c)] for r in ORDER)]
    print(f"{'':<11}" + "".join(f"{c[:5]:>6}" for c in cols))
    for r in ORDER:
        if any(conf[(r, c)] for r in ORDER):
            print(f"{r:<11}" + "".join(f"{conf[(r,c)] or '':>6}" for c in cols))

    print("\nwin% loss range per chess.com label (min / median / max) — set THRESHOLDS between adjacent ranges")
    for lab in ORDER:
        v = sorted(loss_by_label.get(lab, []))
        if v: print(f"{lab:<11} {v[0]:>5.1f} / {v[len(v)//2]:>5.1f} / {v[-1]:>5.1f}   n={len(v)}")

if __name__ == "__main__":
    if len(sys.argv) != 3: sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
