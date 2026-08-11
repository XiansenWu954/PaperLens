"""papers app 单元测试：upsert 去重 + referenced_works 落库。"""
from django.test import TestCase

from papers.models import Paper, Venue, upsert_paper


class UpsertPaperTest(TestCase):
    def test_create_new_paper(self):
        p = upsert_paper({"doi": "10.1/a", "title": "T1", "year": 2024, "referenced_works": ["W1", "W2"]})
        self.assertEqual(Paper.objects.count(), 1)
        self.assertEqual(p.title, "T1")
        self.assertEqual(p.referenced_works, ["W1", "W2"])

    def test_same_doi_updates_not_duplicates(self):
        p1 = upsert_paper({"doi": "10.1/a", "title": "T1", "year": 2024, "citation_count": 1})
        p2 = upsert_paper({"doi": "10.1/a", "title": "T1 v2", "year": 2024, "citation_count": 5})
        self.assertEqual(p1.id, p2.id)
        self.assertEqual(Paper.objects.count(), 1)
        self.assertEqual(p2.title, "T1 v2")
        self.assertEqual(p2.citation_count, 5)

    def test_dedup_by_arxiv_id(self):
        p1 = upsert_paper({"arxiv_id": "2401.123", "title": "A1", "year": 2024})
        p2 = upsert_paper({"arxiv_id": "2401.123", "title": "A1 updated", "year": 2024})
        self.assertEqual(p1.id, p2.id)

    def test_dedup_by_openalex_id(self):
        p1 = upsert_paper({"openalex_id": "W123", "title": "O1", "year": 2023})
        p2 = upsert_paper({"openalex_id": "W123", "title": "O1", "year": 2023})
        self.assertEqual(p1.id, p2.id)

    def test_referenced_works_jsonfield_persistence(self):
        refs = [f"W{i}" for i in range(95)]
        p = upsert_paper({"doi": "10.1/refs", "title": "Refs", "year": 2024, "referenced_works": refs})
        p.refresh_from_db()
        self.assertEqual(len(p.referenced_works), 95)

    def test_doi_normalized_lowercase(self):
        upsert_paper({"doi": "10.1/UPPER", "title": "T", "year": 2024})
        self.assertTrue(Paper.objects.filter(doi="10.1/upper").exists())


class VenueConstraintTest(TestCase):
    def test_venue_unique_name(self):
        from django.db import IntegrityError
        Venue.objects.create(name="NeurIPS")
        with self.assertRaises(IntegrityError):
            Venue.objects.create(name="NeurIPS")


class BibtexParseTest(TestCase):
    """BibTeX / RIS 解析与导出。"""

    BIB = """@article{vaswani2017attention,
  title = {Attention is all you need},
  author = {Vaswani, Ashish and Shazeer, Noam},
  journal = {Advances in Neural Information Processing Systems},
  year = {2017},
  doi = {10.5555/3295222.3295349},
  eprint = {1706.03762}
}
"""

    def test_parse_bibtex_extracts_fields(self):
        from papers.bibtex import parse_bibtex

        payloads = parse_bibtex(self.BIB)
        self.assertEqual(len(payloads), 1)
        p = payloads[0]
        self.assertEqual(p["title"], "Attention is all you need")
        self.assertEqual(p["year"], 2017)
        self.assertEqual(p["doi"], "10.5555/3295222.3295349")
        self.assertEqual(p["arxiv_id"], "1706.03762")
        self.assertEqual(p["venue_name"], "Advances in Neural Information Processing Systems")
        self.assertEqual(p["authors"], ["Vaswani, Ashish", "Shazeer, Noam"])

    def test_parse_empty_returns_empty_list(self):
        from papers.bibtex import parse_bibtex

        self.assertEqual(parse_bibtex(""), [])

    def test_parse_ris_extracts_fields(self):
        from papers.bibtex import parse_ris

        ris = """TY  - JOUR
TI  - Mamba: Linear-Time Sequence Modeling
AU  - Gu, Albert
AU  - Dao, Tri
PY  - 2023
DO  - 10.48550/arXiv.2312.00752
UR  - https://arxiv.org/abs/2312.00752
ER  - """
        payloads = parse_ris(ris)
        self.assertEqual(len(payloads), 1)
        p = payloads[0]
        self.assertEqual(p["title"], "Mamba: Linear-Time Sequence Modeling")
        self.assertEqual(p["year"], 2023)
        self.assertIn("Gu, Albert", p["authors"])

    def test_export_roundtrip_preserves_key_fields(self):
        from papers.bibtex import papers_to_bibtex, parse_bibtex

        p = upsert_paper({
            "doi": "10.1/exp", "arxiv_id": "2401.001",
            "title": "Experiment Paper", "year": 2024, "abstract": "A test abstract",
        })
        bib_text = papers_to_bibtex([p])
        # 导出再导入应能还原核心字段
        reparsed = parse_bibtex(bib_text)[0]
        self.assertEqual(reparsed["title"], "Experiment Paper")
        self.assertEqual(reparsed["doi"], "10.1/exp")
        self.assertEqual(reparsed["year"], 2024)
        self.assertIn("2401.001", bib_text)

    def test_export_ris_format(self):
        from papers.bibtex import papers_to_ris

        p = upsert_paper({"title": "RIS Test", "year": 2024, "doi": "10.1/ris"})
        ris_text = papers_to_ris([p])
        self.assertIn("TY  - JOUR", ris_text)
        self.assertIn("TI  - RIS Test", ris_text)
        self.assertIn("ER  - ", ris_text)
