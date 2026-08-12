import unittest

from find_leads import (
    DEFAULT_SCORING_THRESHOLDS,
    LeadFilters,
    _passes_review_and_rating_filters,
    _passes_website_filter,
    score_lead,
)


class ScoreLeadTests(unittest.TestCase):
    def test_high_few_reviews_with_website(self):
        score, reason = score_lead(rating=4.8, review_count=5, has_website=True)
        self.assertEqual(score, "HIGH")
        self.assertIn("5 review", reason)

    def test_high_rating_sweet_spot_with_website(self):
        score, reason = score_lead(rating=3.7, review_count=200, has_website=True)
        self.assertEqual(score, "HIGH")
        self.assertIn("room to improve", reason)

    def test_no_website_is_low_even_with_few_reviews(self):
        score, reason = score_lead(rating=4.9, review_count=3, has_website=False)
        self.assertEqual(score, "LOW")
        self.assertIn("no website", reason)

    def test_many_reviews_is_low(self):
        score, reason = score_lead(rating=4.5, review_count=250, has_website=True)
        self.assertEqual(score, "LOW")

    def test_medium_band(self):
        score, reason = score_lead(rating=4.6, review_count=25, has_website=True)
        self.assertEqual(score, "MEDIUM")

    def test_gap_defaults_to_medium(self):
        # 50 reviews, has website, rating outside the 3.3-4.2 sweet spot,
        # and outside both the MEDIUM (15-40) and LOW (>100) bands.
        score, reason = score_lead(rating=4.7, review_count=50, has_website=True)
        self.assertEqual(score, "MEDIUM")

    def test_no_rating_no_website_is_low(self):
        score, reason = score_lead(rating=None, review_count=0, has_website=False)
        self.assertEqual(score, "LOW")

    def test_custom_thresholds_override_defaults(self):
        # rating=4.6 is outside the default HIGH sweet spot (3.3-4.2), and
        # 20 reviews is outside the default HIGH review-count cutoff (<15),
        # so with default thresholds this lands in MEDIUM (15-40 band).
        default_score, _ = score_lead(rating=4.6, review_count=20, has_website=True)
        self.assertEqual(default_score, "MEDIUM")

        # Raising the HIGH review-count cutoff to 25 should now catch it.
        custom_thresholds = dict(DEFAULT_SCORING_THRESHOLDS, high_review_count_max=25)
        custom_score, _ = score_lead(rating=4.6, review_count=20, has_website=True, thresholds=custom_thresholds)
        self.assertEqual(custom_score, "HIGH")


class ReviewAndRatingFilterTests(unittest.TestCase):
    def test_no_filters_passes_everything(self):
        self.assertTrue(_passes_review_and_rating_filters(rating=4.0, review_count=5, filters=None))

    def test_min_reviews(self):
        filters = LeadFilters(min_reviews=10)
        self.assertFalse(_passes_review_and_rating_filters(rating=4.0, review_count=5, filters=filters))
        self.assertTrue(_passes_review_and_rating_filters(rating=4.0, review_count=10, filters=filters))

    def test_max_reviews(self):
        filters = LeadFilters(max_reviews=40)
        self.assertTrue(_passes_review_and_rating_filters(rating=4.0, review_count=40, filters=filters))
        self.assertFalse(_passes_review_and_rating_filters(rating=4.0, review_count=41, filters=filters))

    def test_min_rating(self):
        filters = LeadFilters(min_rating=4.0)
        self.assertFalse(_passes_review_and_rating_filters(rating=3.9, review_count=5, filters=filters))
        self.assertTrue(_passes_review_and_rating_filters(rating=4.0, review_count=5, filters=filters))

    def test_min_rating_excludes_missing_rating(self):
        filters = LeadFilters(min_rating=4.0)
        self.assertFalse(_passes_review_and_rating_filters(rating=None, review_count=5, filters=filters))

    def test_max_rating(self):
        filters = LeadFilters(max_rating=4.2)
        self.assertTrue(_passes_review_and_rating_filters(rating=4.2, review_count=5, filters=filters))
        self.assertFalse(_passes_review_and_rating_filters(rating=4.3, review_count=5, filters=filters))

    def test_combined_review_and_rating_filters(self):
        filters = LeadFilters(min_reviews=10, max_reviews=50, min_rating=3.5, max_rating=4.5)
        self.assertTrue(_passes_review_and_rating_filters(rating=4.0, review_count=25, filters=filters))
        self.assertFalse(_passes_review_and_rating_filters(rating=4.0, review_count=5, filters=filters))
        self.assertFalse(_passes_review_and_rating_filters(rating=4.9, review_count=25, filters=filters))


class WebsiteFilterTests(unittest.TestCase):
    def test_no_filter_passes_either_way(self):
        filters = LeadFilters()
        self.assertTrue(_passes_website_filter(True, filters))
        self.assertTrue(_passes_website_filter(False, filters))

    def test_require_website(self):
        filters = LeadFilters(has_website=True)
        self.assertTrue(_passes_website_filter(True, filters))
        self.assertFalse(_passes_website_filter(False, filters))

    def test_require_no_website(self):
        filters = LeadFilters(has_website=False)
        self.assertTrue(_passes_website_filter(False, filters))
        self.assertFalse(_passes_website_filter(True, filters))


if __name__ == "__main__":
    unittest.main()
