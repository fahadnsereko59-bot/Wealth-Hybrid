import ast, pathlib, unittest
ROOT=pathlib.Path(__file__).parent
class PackageTests(unittest.TestCase):
 def test_app_syntax(self): ast.parse((ROOT/'app.py').read_text())
 def test_ai_syntax(self): ast.parse((ROOT/'ai_manager.py').read_text())
 def test_procfile(self): self.assertIn('gunicorn app:app',(ROOT/'Procfile').read_text())
 def test_playwright_dependency(self): self.assertIn('playwright==',(ROOT/'requirements.txt').read_text())
 def test_diagnosis_route(self): self.assertIn('/api/diagnosis',(ROOT/'app.py').read_text())
if __name__=='__main__': unittest.main()
