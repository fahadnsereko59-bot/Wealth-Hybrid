"""Transparent heuristic analysis; not machine learning or a guarantee of outcomes."""
class AIManager:
    def analyze(self, detail):
        stats = detail.get('stats') or {}
        sh = stats.get('shots') or [0, 0]
        sot = stats.get('shots_on_target') or [0, 0]
        home, away = detail.get('home', 'Home'), detail.get('away', 'Away')
        # Conservative, clearly-labelled heuristic market suggestion.
        total_sot = sum(x for x in sot if isinstance(x, (int, float)))
        prob = max(45, min(78, 52 + total_sot * 1.2))
        return {"best_asset": {"market": "Over 0.5 Goals", "prob": round(prob, 1), "odd": round(1 / (prob / 100), 2), "grade": "B" if prob >= 60 else "C"}, "note": "Heuristic estimate from available stats; not a guarantee. Verify live data before use."}
AI_MANAGER = AIManager()
