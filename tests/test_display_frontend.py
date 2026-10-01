"""Static page contract and optional dependency-free Node behavioral checks."""
from html.parser import HTMLParser
from pathlib import Path
import re
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
DISPLAY = ROOT / "raspberry-pi-voice-hub" / "display"


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []
        self.nodes = []
        self.stack = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.tags.append((tag, attrs))
        self.nodes.append((tag, attrs, tuple(self.stack)))
        if tag not in {"meta", "link", "input", "br"}:
            self.stack.append((tag, attrs))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)


class DisplayFrontendTests(unittest.TestCase):
    def setUp(self):
        self.html = (DISPLAY / "index.html").read_text(encoding="utf-8")
        self.parser = PageParser()
        self.parser.feed(self.html)

    def test_four_pages_have_accessible_navigation_and_only_memories_is_upcoming(self):
        pages = [attrs for tag, attrs in self.parser.tags
                 if tag == "section" and "page" in attrs.get("class", "").split()]
        self.assertEqual([page["data-page"] for page in pages],
                         ["home", "radio", "game", "memories"])
        self.assertTrue(all(page.get("aria-label") for page in pages))
        self.assertTrue(all("inert" in page for page in pages[1:]))
        self.assertEqual(pages[2]["aria-label"], "인지 놀이 · 순서 기억")
        self.assertIn("game-page", pages[2]["class"].split())
        self.assertIn("준비 중", pages[3]["aria-label"])
        dots = [attrs for tag, attrs in self.parser.tags
                if tag == "button" and "data-page-index" in attrs]
        self.assertEqual([dot["data-page-index"] for dot in dots], ["0", "1", "2", "3"])

    def test_native_audio_has_no_autoplay_or_bundled_stream_and_volume_is_touch_safe(self):
        media = [attrs for tag, attrs in self.parser.tags if tag == "audio"]
        self.assertEqual(len(media), 1)
        self.assertEqual(media[0].get("preload"), "none")
        self.assertNotIn("autoplay", media[0])
        self.assertNotIn("src", media[0])
        sliders = [attrs for tag, attrs in self.parser.tags if tag == "input"]
        self.assertEqual(sliders[0]["type"], "range")
        self.assertEqual(sliders[0]["aria-label"], "라디오 소리 크기")
        volume_nodes = [(tag, attrs, ancestors) for tag, attrs, ancestors in self.parser.nodes
                        if attrs.get("id") in {"radio-volume", "volume-label"}
                        or (tag == "label" and attrs.get("for") == "radio-volume")]
        self.assertEqual({tag for tag, _, _ in volume_nodes}, {"input", "output", "label"})
        wrappers = [attrs for _, attrs in self.parser.tags if "data-no-swipe" in attrs]
        self.assertTrue(wrappers)
        self.assertTrue(any(all(any(parent is wrapper for _, parent in ancestors)
                                    for _, _, ancestors in volume_nodes)
                            for wrapper in wrappers),
                        "One no-swipe wrapper must contain the slider, label and volume output")

    def test_touch_policy_is_on_the_scrollable_page_and_native_volume_input(self):
        css = (DISPLAY / "styles.css").read_text(encoding="utf-8")
        css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)

        def declarations(selector):
            result = {}
            for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
                if selector in [part.strip() for part in selectors.split(",")]:
                    result.update((key.strip(), value.strip()) for key, value in
                                  (item.split(":", 1) for item in body.split(";") if ":" in item))
            return result

        page = declarations(".page")
        self.assertEqual(page.get("overflow-y"), "auto")
        self.assertEqual(page.get("touch-action"), "pan-y",
                         "Chromium must see horizontal gesture ownership on the page scroll container")
        self.assertEqual(page.get("user-select"), "none")
        self.assertEqual(declarations(".page-viewport").get("touch-action"), "pan-y")
        self.assertEqual(declarations(".volume-control input").get("touch-action"), "none")

    def test_browser_assets_keep_local_entry_points_and_reduced_motion(self):
        scripts = [attrs["src"] for tag, attrs in self.parser.tags if tag == "script"]
        self.assertEqual(scripts, ["/memory_game.js", "/app.js"])
        self.assertIn("prefers-reduced-motion", (DISPLAY / "styles.css").read_text(encoding="utf-8"))
        self.assertIn('id="talk-button"', self.html)
        self.assertIn('id="alert-card"', self.html)

    def test_memory_game_has_three_named_actions_and_accessible_session_feedback(self):
        game_nodes = [(tag, attrs) for tag, attrs, ancestors in self.parser.nodes
                      if any(parent.get("data-page") == "game" for _, parent in ancestors)]
        actions = [(tag, attrs) for tag, attrs in game_nodes if "data-game-action" in attrs]
        self.assertEqual([(tag, attrs.get("id"), attrs["data-game-action"])
                          for tag, attrs in actions],
                         [("button", "game-left", "left"),
                          ("button", "game-right", "right"),
                          ("button", "game-both", "both")])
        for _, attrs in actions:
            self.assertEqual(attrs.get("type"), "button")
            self.assertIn("disabled", attrs, "Actions must wait for the player's input phase")
            self.assertNotIn("onclick", attrs)
        starts = [attrs for tag, attrs in game_nodes if attrs.get("id") == "game-start"]
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0].get("type"), "button")
        feedback = [(attrs, ancestors) for _, attrs, ancestors in self.parser.nodes
                    if attrs.get("id") in {"game-status", "game-progress"}]
        self.assertEqual({attrs["id"] for attrs, _ in feedback}, {"game-status", "game-progress"})
        live_regions = [attrs for _, attrs in game_nodes
                        if attrs.get("role") == "status" and attrs.get("aria-live") == "polite"
                        and attrs.get("aria-atomic") == "true"]
        self.assertTrue(any(all(any(parent is region for _, parent in ancestors)
                                for _, ancestors in feedback) for region in live_regions),
                        "Status and progress must share a polite, atomic live region")
        for identifier in ("game-score", "game-best"):
            self.assertEqual(sum(attrs.get("id") == identifier for _, attrs in game_nodes), 1)
        self.assertFalse(any("placeholder-page" in attrs.get("class", "").split()
                             for _, attrs in game_nodes))

    @unittest.skipUnless(shutil.which("node"), "Node is optional for frontend developer tests")
    def test_browser_behavior_with_mock_media_and_state_api(self):
        result = subprocess.run(
            [shutil.which("node"), "--test",
             str(ROOT / "tests" / "display_browser.test.js"),
             str(ROOT / "tests" / "memory_game.test.js")],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
