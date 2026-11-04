import unittest

from helpers import memory
from reverie.validate import find_secrets, parse_frontmatter, validate_memory

LIMITS = {"file_bytes": 4096, "max_depth": 3}


class ValidateTest(unittest.TestCase):
    def test_parses_scalars_and_lists(self):
        meta, body = parse_frontmatter("---\nname: a\nevidence: [s-1, 's-2']\ntags:\n  - x\n  - y\n---\n\nbody\n")
        self.assertEqual(meta, {"name": "a", "evidence": ["s-1", "s-2"], "tags": ["x", "y"]})
        self.assertEqual(body, "body\n")

    def test_valid_memory_passes(self):
        self.assertEqual(validate_memory("testing/vitest-mocks.md", memory("vitest-mocks"), LIMITS), [])

    def test_rejects_structure_problems(self):
        problems = validate_memory("A/B/c/d.md", "no frontmatter", LIMITS)
        self.assertTrue(any("deeper" in p for p in problems))
        self.assertTrue(any("kebab" in p for p in problems))
        self.assertTrue(any("frontmatter" in p for p in problems))

    def test_rejects_unknown_type_and_oversize(self):
        problems = validate_memory("a.md", memory("a", type_="gossip") + "x" * 5000, LIMITS)
        self.assertTrue(any("type must be" in p for p in problems))
        self.assertTrue(any("bytes" in p for p in problems))

    def test_detects_secrets(self):
        self.assertIn("GitHub token", find_secrets("token ghp_" + "a" * 36))
        self.assertIn("AWS access key", find_secrets("AKIAABCDEFGHIJKLMNOP"))
        self.assertIn("Credential assignment", find_secrets("password = hunter2hunter2"))
        self.assertEqual(find_secrets("use the password reset flow"), [])
        for leaked in ("aws_secret_access_key = wJalrXUtnFEMI/K7MDENG", "SECRET_KEY=abcdefgh1234",
                       "DATABASE_PASSWORD=hunter2hunter2", "postgres://admin:pw@db.internal/app"):
            self.assertTrue(find_secrets(leaked), leaked)
        for benign in ("max_tokens: 2000", "max_tokens = 100000000", "token: process.env.GITHUB_TOKEN",
                       "tokenizer = AutoTokenizer.from_pretrained", "pwd=$(pwd)/build",
                       "CSRF_TOKEN_HEADER: X-CSRF-Token-Value", "secret: ${{ secrets.DEPLOY_KEY }}"):
            self.assertEqual(find_secrets(benign), [], benign)

    def test_caps_description_length(self):
        text = memory("a").replace("description: about a", "description: " + "x" * 300)
        problems = validate_memory("a.md", text, LIMITS)
        self.assertIn("description is 300 characters", " ".join(problems))


if __name__ == "__main__":
    unittest.main()
