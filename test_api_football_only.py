import ast, pathlib, unittest
APP = pathlib.Path(__file__).with_name('app.py').read_text()
class TestApiFootballOnly(unittest.TestCase):
    def test_python_parses(self): ast.parse(APP)
    def test_no_espn_reference(self): self.assertNotIn('espn.com', APP.lower())
    def test_api_football_endpoint(self): self.assertIn('https://v3.football.api-sports.io', APP); self.assertIn('Africa/Kampala', APP)
    def test_timezone_on_requests(self): self.assertIn('query["timezone"] = "Africa/Kampala"', APP)
    def test_sqlite_cache_and_usage(self): self.assertIn('CREATE TABLE IF NOT EXISTS cache', APP); self.assertIn('CREATE TABLE IF NOT EXISTS api_usage', APP)
    def test_shared_quota_cap(self): self.assertIn('def _reserve_api_call', APP); self.assertIn('API_FOOTBALL_DAILY_CAP', APP)
    def test_all_fixtures_not_truncated_to_20(self): self.assertIn('return normalized', APP); self.assertNotIn('return normalized[:20]', APP)
    def test_team_history_and_h2h(self): self.assertIn('def api_football_team_history', APP); self.assertIn('def api_football_head_to_head', APP); self.assertIn('last', APP)
    def test_railway_port(self): self.assertIn('os.environ.get("PORT",5000)', APP)
    def test_no_fake_fixtures(self): self.assertNotIn('fallback hardcoded 27', APP.lower())
if __name__ == '__main__': unittest.main()
