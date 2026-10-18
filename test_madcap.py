import tempfile
import unittest
from pathlib import Path
from selectolax.parser import HTMLParser
from markdownify import MarkdownConverter

from .utils import (
    strip_lang_suffix,
    manual_basename_from_zip,
    sanitize_filename,
    is_shell_path,
)
from .toc import (
    parse_define_js,
    _looks_like_topic_path,
)
from .converter import (
    FlareMarkdownConverter,
    ImageProcessingState,
    convert_topic_body,
    preclean_html_text,
    _replace_with_text,
    rewrite_xrefs,
    _merge_adjacent_code_fences,
    slugify_for_anchor,
)
from .validator import validate_markdown_syntax

class TestMadCapUtils(unittest.TestCase):
    def test_strip_lang_suffix(self):
        self.assertEqual(strip_lang_suffix("OneIM_en-us.html5"), "OneIM")
        self.assertEqual(strip_lang_suffix("OneIM_de-de.html5"), "OneIM")
        self.assertEqual(strip_lang_suffix("OneIM_FR-FR.html5"), "OneIM")
        self.assertEqual(strip_lang_suffix("OneIM"), "OneIM")

    def test_manual_basename_from_zip(self):
        self.assertEqual(manual_basename_from_zip(Path("/tmp/OneIM_en-us.html5.zip")), "OneIM")
        self.assertEqual(manual_basename_from_zip(Path("OneIM_en-us.html5")), "OneIM")
        self.assertEqual(manual_basename_from_zip(Path("MyManual.zip")), "MyManual")
        self.assertEqual(manual_basename_from_zip(Path("SimpleName.html5")), "SimpleName")

    def test_sanitize_filename(self):
        self.assertEqual(sanitize_filename("Image One!"), "Image_One")
        self.assertEqual(sanitize_filename("Image-1.2.png"), "Image-1.2.png")
        self.assertEqual(sanitize_filename("   "), "image")

    def test_is_shell_path(self):
        root = Path("/manual_root")
        self.assertTrue(is_shell_path(root / "something_csh.html", root))
        self.assertTrue(is_shell_path(root / "something_csh.htm", root))
        # Matches parent directory name
        self.assertTrue(is_shell_path(root / "manual_root.html", root))
        self.assertTrue(is_shell_path(root / "manual_root.htm", root))
        # Non-shell path
        self.assertFalse(is_shell_path(root / "topic.html", root))
        self.assertFalse(is_shell_path(root / "Sources/topic.html", root))

class TestMadCapTOC(unittest.TestCase):
    def test_parse_define_js(self):
        js_amd = 'define({ "prefix": "Chunk", "tree": { "n": [] } });'
        parsed = parse_define_js(js_amd)
        self.assertEqual(parsed.get("prefix"), "Chunk")

        js_amd_spaces = '\n  define( \n  { "foo": "bar" }\n  ) \n  '
        parsed = parse_define_js(js_amd_spaces)
        self.assertEqual(parsed.get("foo"), "bar")

        js_raw = '{ "foo": "baz" }'
        parsed = parse_define_js(js_raw)
        self.assertEqual(parsed.get("foo"), "baz")

    def test_looks_like_topic_path(self):
        self.assertTrue(_looks_like_topic_path("Sources/folder/topic.html"))
        self.assertTrue(_looks_like_topic_path("Data/folder/topic.htm"))
        self.assertFalse(_looks_like_topic_path(""))
        self.assertFalse(_looks_like_topic_path("___"))
        self.assertFalse(_looks_like_topic_path("Sources"))

class TestMadCapConverter(unittest.TestCase):
    def test_preclean_html_text(self):
        raw_html = '<?xml version="1.0" encoding="utf-8"?><p>hello <placeholder> world \\U1234 \\u5678</p>'
        cleaned = preclean_html_text(raw_html)
        self.assertNotIn("<?xml", cleaned)
        self.assertIn("&lt;placeholder&gt;", cleaned)
        # Check backslash escaping
        self.assertIn("\\\\U1234", cleaned)
        self.assertIn("\\\\u5678", cleaned)

    def test_replace_with_text_escaping(self):
        tree = HTMLParser('<div><img src="icon.png" alt="Icon" /></div>')
        img = tree.css_first("img")
        _replace_with_text(img, "[Image omitted: <test & check>]")
        
        # Verify selectolax HTML output contains escaped characters
        html_out = tree.html
        self.assertIn("[Image omitted: &lt;test &amp; check&gt;]", html_out)
        
        # Verify markdownify parses it back to clean text
        md_out = MarkdownConverter().convert(html_out)
        self.assertEqual(md_out.strip(), "[Image omitted: <test & check>]")

    def test_merge_adjacent_code_fences(self):
        md = "```\ncode block 1\n```\n\n```\ncode block 2\n```"
        merged = _merge_adjacent_code_fences(md)
        self.assertEqual(merged, "```\ncode block 1\ncode block 2\n```")

    def test_slugify_for_anchor(self):
        self.assertEqual(slugify_for_anchor("Introduction & Overview"), "introduction-overview")
        self.assertEqual(slugify_for_anchor("Title --- with __ symbols"), "title-with-symbols")
        self.assertEqual(slugify_for_anchor("!!!"), "section")

    def test_rewrite_xrefs(self):
        # We need a dummy directory setup
        root = Path("/manual_root").resolve()
        topic = root / "Sources" / "Folder1" / "TopicA.html"
        
        topic_id_map = {
            "sources/folder1/topicb.html": "sources-folder1-topicb-htm-abc12345",
        }
        
        # Cross reference linking to TopicB in same folder
        html = '<div><a href="TopicB.html">Link to B</a><a href="http://google.com">Google</a><a href="Missing.html">Missing</a></div>'
        tree = HTMLParser(html)
        body = tree.css_first("div")
        
        rewrite_xrefs(body, topic, root, topic_id_map)
        
        links = body.css("a")
        # There should only be 2 links remaining because the missing one was unwrapped
        self.assertEqual(len(links), 2)
        # TopicB link should be mapped to the topic ID
        self.assertEqual(links[0].attributes.get("href"), "#topic-sources-folder1-topicb-htm-abc12345")
        # Google link should remain untouched
        self.assertEqual(links[1].attributes.get("href"), "http://google.com")
        # Verify body text still contains all text
        self.assertIn("Missing", body.text())

    def test_convert_topic_body_strips_code_snippet_toolbar_and_line_numbers(self):
        html = """<!DOCTYPE html>
<html><body><div id="mc-main-content">
    <h1>Topic Title</h1>
    <div class="Example">
        <div class="codeSnippet">
            <a class="codeSnippetCopyButton" role="button" href="javascript:void(0);">Copy</a>
            <div class="codeSnippetCaption"><p>Example</p></div>
            <div class="codeSnippetBody" data-mc-continue="False" data-mc-line-number-start="1" data-mc-use-line-numbers="True"><pre><span class="codeSnippetLineNumbers" style="border-right: #AAA 2px solid;" aria-hidden="true">1
2
3
4
</span><code>&#160;&#160;&#160;&#160;public class ExampleApi<br />&#160;&#160;&#160;&#160;{<br />&#160;&#160;&#160;&#160;&#160;&#160;&#160;&#160;public void Build() { }<br />&#160;&#160;&#160;&#160;}</code></pre>
            </div>
        </div>
    </div>
</div></body></html>"""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            topic = root / "Sources" / "Topic.htm"
            topic.parent.mkdir(parents=True, exist_ok=True)
            topic.write_text(html, encoding="utf-8")

            md = convert_topic_body(topic, root, {}, ImageProcessingState(), "strip")
            self.assertEqual(
                md,
                "Example\n\n```\npublic class ExampleApi\n{\n    public void Build() { }\n}\n```",
            )

    def test_convert_pre_handles_empty_mixed_indent_and_code_language(self):
        conv = FlareMarkdownConverter(heading_style="ATX")
        self.assertEqual(conv.convert("<pre></pre>").strip(), "")
        self.assertEqual(conv.convert("<pre>   \n  \n</pre>").strip(), "")

        # Mixed tab and space prefixes share no common whitespace prefix and must not be mangled
        mixed = conv.convert("<pre><code>\ta\n    b</code></pre>").strip()
        self.assertEqual(mixed, "```\n\ta\n    b\n```")

        # Honors code_language and code_language_callback options
        conv_lang = FlareMarkdownConverter(heading_style="ATX", code_language="csharp")
        self.assertEqual(
            conv_lang.convert("<pre><code>var x = 1;</code></pre>").strip(),
            "```csharp\nvar x = 1;\n```",
        )
        conv_cb = FlareMarkdownConverter(
            heading_style="ATX",
            code_language_callback=lambda el: "vb" if el else None,
        )
        self.assertEqual(
            conv_cb.convert("<pre><code>Dim x As Integer</code></pre>").strip(),
            "```vb\nDim x As Integer\n```",
        )

class TestMarkdownValidation(unittest.TestCase):
    def test_validate_markdown_syntax_valid(self):
        md = "# Header\n\nThis is a paragraph.\n\n```python\nprint('hello')\n```\n"
        passed, err = validate_markdown_syntax(md)
        self.assertTrue(passed)
        self.assertIsNone(err)

    def test_validate_markdown_syntax_invalid_fences(self):
        # Odd number of fences
        md = "Some text\n\n```python\nprint('hello')\n"
        passed, err = validate_markdown_syntax(md)
        self.assertFalse(passed)
        self.assertEqual(err, "Unbalanced code block fences (odd number of ```)")

if __name__ == "__main__":
    unittest.main()
