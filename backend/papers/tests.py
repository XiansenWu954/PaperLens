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
