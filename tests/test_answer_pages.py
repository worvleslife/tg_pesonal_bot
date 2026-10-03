import unittest
from html import escape

from assistant_bot.answer_pages import paginate_answer


class AnswerPageTests(unittest.TestCase):
    def assert_valid_pages(self, text, *, limit=4096):
        pages = paginate_answer(text, limit)
        self.assertEqual("".join(pages), text)
        for page in pages:
            self.assertTrue(page)
            self.assertLessEqual(len(page.encode("utf-16-le")) // 2, limit)
        return pages

    def test_cyrillic_at_telegram_boundaries(self):
        for length, expected_lengths in [(4095, [4095]), (4096, [4096]),
                                         (4097, [4096, 1])]:
            with self.subTest(length=length):
                pages = self.assert_valid_pages("Я" * length)
                self.assertEqual(list(map(len, pages)), expected_lengths)

    def test_emoji_count_as_two_units_and_stay_intact(self):
        self.assertEqual(self.assert_valid_pages("😀" * 2048), ["😀" * 2048])
        self.assertEqual(self.assert_valid_pages("😀" * 2049), ["😀" * 2048, "😀"])
        self.assertEqual(self.assert_valid_pages("а" * 4095 + "😀"),
                         ["а" * 4095, "😀"])
        self.assertEqual(self.assert_valid_pages("а😀б😀", limit=3), ["а😀", "б😀"])

    def test_html_is_plain_text_before_escaping(self):
        text = '<tag attr="значение">&' * 180
        pages = self.assert_valid_pages(text)
        self.assertEqual(pages, [text])
        self.assertGreater(len(escape(text)), 4096)

    def test_paragraph_has_priority_over_later_space(self):
        text = "а" * 64 + "\n\n" + "б" * 24 + " " + "в" * 50
        pages = self.assert_valid_pages(text, limit=100)
        self.assertEqual(pages[0], "а" * 64 + "\n\n")

    def test_crlf_paragraph_is_preserved(self):
        text = "а" * 64 + "\r\n\r\n" + "б" * 100
        self.assertEqual(self.assert_valid_pages(text, limit=100)[0],
                         "а" * 64 + "\r\n\r\n")

    def test_newline_has_priority_over_later_space(self):
        text = "а" * 65 + "\n" + "б" * 25 + " " + "в" * 50
        self.assertEqual(self.assert_valid_pages(text, limit=100)[0], "а" * 65 + "\n")

    def test_spaces_and_tabs_are_kept(self):
        for separator in (" ", "\t"):
            with self.subTest(separator=separator):
                text = "а" * 70 + separator + "б" * 80
                self.assertEqual(self.assert_valid_pages(text, limit=100)[0],
                                 "а" * 70 + separator)

    def test_early_separator_does_not_make_mostly_empty_page(self):
        text = "а" * 58 + "\n\n" + "б" * 90
        pages = self.assert_valid_pages(text, limit=100)
        self.assertEqual(len(pages[0]), 100)

    def test_preferred_break_uses_units_not_codepoint_count(self):
        text = "😀" * 32 + "\n\n" + "а" * 70
        self.assertEqual(self.assert_valid_pages(text, limit=100)[0],
                         "😀" * 32 + "\n\n")

    def test_short_text_is_not_split_at_whitespace(self):
        text = "  Привет!\n\nЭто весь ответ.\n "
        self.assertEqual(self.assert_valid_pages(text), [text])

    def test_long_unbroken_answer(self):
        pages = self.assert_valid_pages("ж" * 20000)
        self.assertEqual(list(map(len, pages)), [4096, 4096, 4096, 4096, 3616])

    def test_small_positive_limit_makes_progress(self):
        self.assertEqual(self.assert_valid_pages("а б", limit=1), ["а", " ", "б"])
        self.assertEqual(self.assert_valid_pages("😀😀", limit=2), ["😀", "😀"])

    def test_impossible_emoji_limit_is_rejected(self):
        with self.assertRaises(ValueError):
            paginate_answer("а😀", 1)

    def test_invalid_limits_are_rejected(self):
        for limit in (0, -1, 1.5, "4096", None, True):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                paginate_answer("ответ", limit)

    def test_empty_text_returns_no_pages(self):
        self.assertEqual(paginate_answer(""), [])


if __name__ == "__main__":
    unittest.main()
