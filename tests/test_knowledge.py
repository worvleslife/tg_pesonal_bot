import unittest

from assistant_bot.knowledge import organize


class KnowledgeOrganizationTests(unittest.TestCase):
    def test_cyrillic_category_and_user_tags(self):
        result = organize("Заметки по Python #Разработка #УЧЁБА #разработка")
        self.assertEqual(result["category"], "Технологии")
        self.assertEqual(result["tags"], ["разработка", "учеба"])
        self.assertEqual(result["title"], "Заметки по Python")

    def test_urls_are_extracted_normalized_and_deduplicated(self):
        result = organize("Код: https://GitHub.com/openai. Ещё https://github.com/openai", urls=["https://github.com/openai"])
        self.assertEqual(result["urls"], ["https://github.com/openai"])
        self.assertEqual(result["category"], "Технологии")

    def test_link_only_has_readable_title(self):
        result = organize("https://stepik.org/course/123")
        self.assertEqual(result["title"], "stepik.org/course/123")
        self.assertEqual(result["category"], "Обучение")

    def test_matching_domain_must_be_true_suffix(self):
        self.assertEqual(organize("https://github.com.attacker.example/page")["category"], "Входящие")
        self.assertEqual(organize("https://docs.github.com/page")["category"], "Технологии")

    def test_unknown_material_goes_into_inbox(self):
        result = organize("Интересный материал")
        self.assertEqual(result["category"], "Входящие")
        self.assertEqual(result["tags"], [])

    def test_categories(self):
        for text, expected in [
            ("Бюджет и расходы", "Финансы"), ("Здоровье и тренировки", "Здоровье"),
            ("Совещание с клиентом", "Работа"), ("Идея для выходных", "Идеи"),
            ("Рецепт для семьи", "Личное"), ("Лекции и экзамены", "Обучение"),
        ]:
            with self.subTest(text=text):
                self.assertEqual(organize(text)["category"], expected)

    def test_filenames_are_used_without_opening_documents(self):
        result = organize("", kind="document", file_name="Лекции по математике.pdf")
        self.assertEqual(result["title"], "Лекции по математике.pdf")
        self.assertEqual(result["category"], "Обучение")
        self.assertEqual(organize("", kind="photo")["title"], "Фото")

    def test_title_uses_first_line_and_is_limited(self):
        self.assertEqual(organize("Первая строка\nВторая строка")["title"], "Первая строка")
        title = organize("Я" * 200)["title"]
        self.assertLessEqual(len(title), 100)
        self.assertTrue(title.endswith("…"))

    def test_balanced_url_parentheses_are_preserved(self):
        result = organize("(https://en.wikipedia.org/wiki/Python_(programming_language)).")
        self.assertEqual(result["urls"], ["https://en.wikipedia.org/wiki/Python_(programming_language)"])

    def test_invalid_and_non_http_urls_are_ignored(self):
        result = organize("Материал", urls=["file:///etc/passwd", "javascript:alert(1)", "https://", "https://example.org:bad/", "https://[invalid"])
        self.assertEqual(result["urls"], [])

    def test_short_keywords_do_not_match_inside_words(self):
        self.assertEqual(organize("Крокодил")["category"], "Входящие")

    def test_links_with_empty_and_root_path_are_duplicates(self):
        result = organize("https://example.org https://example.org/")
        self.assertEqual(len(result["urls"]), 1)

    def test_url_fragments_are_not_user_hashtags(self):
        result = organize("https://example.org/#part #Моё")
        self.assertEqual(result["tags"], ["мое"])

    def test_credentials_in_user_supplied_links_are_not_changed(self):
        result = organize("https://User:Pass@EXAMPLE.org/page")
        self.assertEqual(result["urls"], ["https://User:Pass@example.org/page"])


if __name__ == "__main__":
    unittest.main()
