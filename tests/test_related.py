import pytest

from app.related import NoteDoc, build_index, graph_edges, rank_related, related_notes, stem, terms


def doc(note_id, title, markdown="", items=(), category=None, updated=1.0):
    return NoteDoc(note_id, title, markdown, tuple(items), category, updated)


CORPUS = [
    doc("launch", "Запуск проекта", "Оплата ещё не готова, запуск в пятницу.", ["Проверить оплату"], "work", 5),
    doc("payment", "Оплата в проекте", "Подключить платёжный модуль, проверить оплату картой.", [], "work", 4),
    doc("design", "Идеи по дизайну", "Цвета и шрифты для проекта.", [], "work", 3),
    doc("borsch", "Борщ", "Свёкла, капуста, мясо. Варить час.", ["Купить свёклу"], None, 2),
    doc("shopping", "Купить свёклу и капусту", "Для борща.", [], None, 1),
]


@pytest.mark.parametrize(("words", "root"), [
    (["проект", "проекта", "проекты", "проектом", "проектов"], "проект"),
    (["оплата", "оплату", "оплаты", "оплатить"], "оплат"),
    (["свёкла", "свеклу", "свёклы"], "свекл"),
    (["разработка", "разработки", "разработку"], "разраб"),
    (["notes", "note"], "note"),
])
def test_forms_of_one_word_share_a_stem(words, root):
    assert {stem(word.replace("ё", "е")) for word in words} == {root}


def test_terms_drop_stop_words_digits_and_short_words_and_keep_original_spelling():
    found = terms("Это ПЛАН на 2026 год: купить свёклу и до 5 штук")
    assert [word for _, word in found] == ["план", "год", "купить", "свёклу", "штук"]


def test_ranking_prefers_shared_meaning_and_hides_unrelated_notes():
    hits = rank_related(CORPUS, "launch")
    assert [hit["note_id"] for hit in hits][:2] == ["payment", "design"]
    assert "borsch" not in [hit["note_id"] for hit in hits]
    assert hits[0]["score"] > hits[1]["score"] >= 0.12
    assert all(0 < hit["score"] <= 1 for hit in hits)
    assert "оплату" in hits[0]["shared_terms"] or "оплата" in hits[0]["shared_terms"]


def test_shared_terms_are_readable_words_not_stems_and_capped():
    hits = rank_related(CORPUS, "borsch")
    assert hits[0]["note_id"] == "shopping"
    shared = hits[0]["shared_terms"]
    assert {"свёкла", "свёклу"} & set(shared) and "капуста" in shared and "борщ" in shared
    assert all(len(term) > 3 for term in shared)
    long = doc("x", " ".join("слово%d" % i for i in range(20)))
    same = doc("y", " ".join("слово%d" % i for i in range(20)))
    assert len(rank_related([long, same], "x")[0]["shared_terms"]) == 5


def test_limit_threshold_and_unknown_note():
    assert len(rank_related(CORPUS, "launch", limit=1)) == 1
    assert rank_related(CORPUS, "launch", min_score=0.99) == []
    assert rank_related(CORPUS, "missing") == []
    assert rank_related([], "launch") == []
    assert rank_related([CORPUS[0]], "launch") == []


def test_item_texts_count_and_title_outweighs_body():
    left = doc("a", "Заметка", "", ["позвонить стоматологу"])
    right = doc("b", "Другая", "", ["записаться к стоматологу"])
    assert [hit["note_id"] for hit in rank_related([left, right], "a")] == ["b"]
    target = doc("t", "Бюджет", "")
    in_title = doc("title", "Бюджет", "прочее")
    in_body = doc("body", "Прочее", "бюджет")
    hits = rank_related([target, in_title, in_body], "t", min_score=0.01)
    assert [hit["note_id"] for hit in hits] == ["title", "body"]


def test_same_category_adds_a_bonus_but_not_a_match_by_itself():
    a = doc("a", "Бюджет месяца", "", category="c")
    same = doc("same", "Бюджет недели", "", category="c")
    other = doc("other", "Бюджет недели", "", category="d")
    hits = {hit["note_id"]: hit["score"] for hit in rank_related([a, same, other], "a")}
    assert hits["same"] > hits["other"]
    unrelated = doc("unrelated", "Рецепт борща", "", category="c")
    assert rank_related([a, unrelated], "a") == []


def test_graph_keeps_three_neighbours_without_duplicate_pairs():
    base = "общая тема"
    docs = [doc(str(i), base + " слово%d" % i, "", updated=i) for i in range(6)]
    edges = graph_edges(build_index(docs), [d.id for d in docs], per_note=3, min_score=0.01)
    pairs = [(e["source"], e["target"]) for e in edges]
    assert len(pairs) == len(set(pairs)) and all(a < b for a, b in pairs)
    assert all(0 < e["score"] <= 1 for e in edges)
    degree = {d.id: sum(d.id in pair for pair in pairs) for d in docs}
    assert max(degree.values()) >= 3
    # Each note nominates at most three, so 6 notes give at most 18 nominations.
    assert len(pairs) <= 18


def test_graph_edges_stay_inside_the_requested_nodes():
    index = build_index(CORPUS)
    edges = graph_edges(index, ["launch", "payment"])
    assert edges == [e for e in edges if {e["source"], e["target"]} <= {"launch", "payment"}]
    assert len(edges) == 1
    assert graph_edges(index, ["borsch"], min_score=0.99) == []
    assert graph_edges(index, ["missing"]) == []


def test_related_notes_never_returns_the_note_itself_and_is_stable():
    index = build_index(CORPUS)
    first = related_notes(index, "payment")
    assert "payment" not in [hit["note_id"] for hit in first]
    assert first == related_notes(build_index(list(reversed(CORPUS))), "payment")


def test_thousand_notes_are_handled():
    docs = [
        doc(str(i), "Заметка о теме%d" % (i % 40), "текст слово%d слово%d" % (i % 90, i % 33), updated=i)
        for i in range(1000)
    ]
    index = build_index(docs)
    assert len(related_notes(index, "7", limit=5)) == 5
    edges = graph_edges(index, [d.id for d in docs])
    assert 0 < len(edges) <= 3000
