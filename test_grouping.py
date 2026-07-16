"""
test_grouping.py — Task 2 unit tests for the grouping engine.

Covers every case listed in spec section 5 "Additional required unit tests":
  1. Crossed-out replacement
  2. Suffix-wins
  3. Suffix-ambiguity flagging
  4. Inheritance across photo pages
  5. Orphan leading page
  6. Non-contiguous merge + flag
  7. Edit-distance-1 whitelist match with unique candidate
  8. Edit-distance-1 with two candidates → flag
  9. Edit-distance-2 detection between same-ticket neighbors → UNMATCHED_NUMBER + neighbor suggestion (added at Gate 1)

Plus integration test: the 15/16-page test file detection results → correct 5 blocks.

NO API CALLS in any test.
"""

import unittest
from grouping import (
    group_detections,
    parse_whitelist,
    digit_edit_distance,
    fuzzy_match_whitelist,
    FLAG_UNMATCHED_NUMBER,
    FLAG_AMBIGUOUS_SUFFIX,
    FLAG_CORRECTION_CONFLICT,
    FLAG_CORRECTION_OBSERVED,
    FLAG_ORPHAN_LEADING_PAGES,
    FLAG_NON_CONTIGUOUS,
    FLAG_MISSING_TICKET,
    FLAG_LOW_CONFIDENCE,
    FLAG_FUZZY_RESOLVED,
    FLAG_SECOND_PASS,
    FLAG_INHERITED_UNMATCHED,
    FLAG_AMBIGUOUS_MATCH,
    HARD_FLAGS,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def make_page(page_num, value=None, source="printed", confidence=0.95,
              crossed_out=False, corrected_from="", second_pass=False, pink_marker=False):
    """Build a raw detection dict for one page."""
    candidates = []
    if value is not None:
        candidates.append({
            "value": value,
            "source": source,
            "confidence": confidence,
            "crossed_out": crossed_out,
            "corrected_from": corrected_from,
            "second_pass": second_pass,
        })
    return {"page": page_num, "candidates": candidates, "pink_marker": pink_marker, "error": None}


def make_pages(*specs):
    """
    Build a list of raw detection dicts.
    Each spec is either:
      - None  → empty page (no candidates)
      - str   → value with default source/confidence
      - dict  → full spec passed to make_page
    """
    result = []
    for i, spec in enumerate(specs, start=1):
        if spec is None:
            result.append(make_page(i))
        elif isinstance(spec, str):
            result.append(make_page(i, value=spec))
        elif isinstance(spec, dict):
            result.append(make_page(i, **spec))
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Unit tests
# ─────────────────────────────────────────────────────────────────────────────

class TestEditDistance(unittest.TestCase):
    def test_identical(self):
        self.assertEqual(digit_edit_distance("253983", "253983"), 0)

    def test_one_substitution(self):
        self.assertEqual(digit_edit_distance("243983", "253983"), 1)

    def test_one_insertion(self):
        self.assertEqual(digit_edit_distance("25398", "253983"), 1)

    def test_one_deletion(self):
        self.assertEqual(digit_edit_distance("2539830", "253983"), 1)

    def test_two_substitutions(self):
        self.assertEqual(digit_edit_distance("247983", "253983"), 2)

    def test_empty(self):
        self.assertEqual(digit_edit_distance("", ""), 0)
        self.assertEqual(digit_edit_distance("", "12345"), 5)


class TestFuzzyMatch(unittest.TestCase):
    WL = ["301532", "257535", "253983", "258066", "257086"]

    def test_exact_match(self):
        self.assertEqual(fuzzy_match_whitelist("253983", self.WL), ["253983"])

    def test_edit_distance_1_unique(self):
        # 243983 → 253983 (distance 1, unique)
        result = fuzzy_match_whitelist("243983", self.WL)
        self.assertEqual(result, ["253983"])

    def test_edit_distance_1_two_candidates(self):
        # 257086 and 257535 are both distance 1 from 257286? Let's construct a real case.
        # 257535 vs 257086: distance is 3. Let's use a whitelist with two close entries.
        wl = ["123456", "123556"]
        # 123456 → 123556: substitution at pos 4 (4→5), distance 1
        # 123466 → 123456 distance 1, 123466 → 123556 distance 2
        result = fuzzy_match_whitelist("123456", wl)
        self.assertEqual(result, ["123456"])  # exact match

        # Now test a value equidistant from two entries
        result2 = fuzzy_match_whitelist("123506", wl)
        # 123506 → 123456: sub at pos 4 (5→4) + sub at pos 5 (0→5) = distance 2? Let's check
        # Actually: 123506 vs 123456: pos3=5vs4, pos4=0vs5 → distance 2
        # 123506 vs 123556: pos3=5vs5 ok, pos4=0vs5 → distance 1
        self.assertEqual(result2, ["123556"])

    def test_edit_distance_2_no_match(self):
        # 247983 → 253983 is distance 2 → no match
        result = fuzzy_match_whitelist("247983", self.WL)
        self.assertEqual(result, [])

    def test_no_match(self):
        result = fuzzy_match_whitelist("999999", self.WL)
        self.assertEqual(result, [])


class TestParseWhitelist(unittest.TestCase):
    def test_comma_separated(self):
        result = parse_whitelist("301532, 257535, 253983")
        self.assertEqual(result, ["301532", "257535", "253983"])

    def test_newline_separated(self):
        result = parse_whitelist("301532\n257535\n253983")
        self.assertEqual(result, ["301532", "257535", "253983"])

    def test_with_suffix(self):
        result = parse_whitelist("12345-1, 12345-2")
        self.assertEqual(result, ["12345-1", "12345-2"])

    def test_invalid_raises(self):
        with self.assertRaises(ValueError):
            parse_whitelist("301532, INVALID, 253983")

    def test_too_short(self):
        with self.assertRaises(ValueError):
            parse_whitelist("1234")  # 4 digits — too short

    def test_too_long(self):
        with self.assertRaises(ValueError):
            parse_whitelist("12345678")  # 8 digits — too long


# ─────────────────────────────────────────────────────────────────────────────
# Test 1: Crossed-out replacement
# ─────────────────────────────────────────────────────────────────────────────

class TestCrossedOutReplacement(unittest.TestCase):
    def test_crossed_out_identity_is_replacement(self):
        """The page's identity must be the replacement, not the crossed-out value.
        When the old value is NOT in the whitelist (simple handwriting correction),
        the flag is CORRECTION_OBSERVED."""
        # Use a whitelist where only the replacement (301532) is valid.
        # The crossed-out value (999999) is not in the whitelist.
        whitelist = ["301532"]
        pages = [
            make_page(1, value="301532"),
            # Page 2: 999999 is crossed out (not a valid ticket), 301532 is the replacement
            {
                "page": 2,
                "candidates": [
                    {"value": "999999", "source": "handwritten", "confidence": 0.95,
                     "crossed_out": True, "corrected_from": ""},
                    {"value": "301532", "source": "handwritten", "confidence": 0.90,
                     "crossed_out": False, "corrected_from": "999999"},
                ],
                "error": None,
            },
        ]
        result = group_detections(pages, whitelist)
        # Both pages should resolve to 301532
        self.assertEqual(len(result.blocks), 1)
        self.assertEqual(result.blocks[0].ticket, "301532")
        self.assertEqual(result.blocks[0].pages, [1, 2])
        self.assertIn(FLAG_CORRECTION_OBSERVED, result.blocks[0].flags)

    def test_crossed_out_conflict(self):
        """If the crossed-out value is also in the whitelist → CORRECTION_CONFLICT."""
        whitelist = ["301532", "257535"]
        pages = [
            {
                "page": 1,
                "candidates": [
                    {"value": "257535", "source": "printed", "confidence": 0.95,
                     "crossed_out": True, "corrected_from": ""},
                    {"value": "301532", "source": "handwritten", "confidence": 0.90,
                     "crossed_out": False, "corrected_from": "257535"},
                ],
                "error": None,
            },
        ]
        result = group_detections(pages, whitelist)
        self.assertEqual(result.blocks[0].ticket, "301532")
        self.assertIn(FLAG_CORRECTION_CONFLICT, result.blocks[0].flags)


# ─────────────────────────────────────────────────────────────────────────────
# Test 2: Suffix wins
# ─────────────────────────────────────────────────────────────────────────────

class TestSuffixWins(unittest.TestCase):
    def test_suffixed_beats_base(self):
        """When both base and suffixed are detected, suffixed wins."""
        whitelist = ["12345", "12345-1"]
        pages = [
            {
                "page": 1,
                "candidates": [
                    {"value": "12345", "source": "printed", "confidence": 0.95,
                     "crossed_out": False, "corrected_from": ""},
                    {"value": "12345-1", "source": "handwritten", "confidence": 0.88,
                     "crossed_out": False, "corrected_from": ""},
                ],
                "error": None,
            },
        ]
        result = group_detections(pages, whitelist)
        self.assertEqual(result.blocks[0].ticket, "12345-1")

    def test_base_only_when_no_suffix_in_whitelist(self):
        """If whitelist has only the base, a stray suffixed reading → UNMATCHED."""
        whitelist = ["12345"]
        pages = [
            make_page(1, value="12345-1"),
        ]
        result = group_detections(pages, whitelist)
        # 12345-1 is not in whitelist; fuzzy: distance("12345-1","12345") = 2 → no match
        self.assertIsNone(result.blocks[0].ticket)
        self.assertIn(FLAG_UNMATCHED_NUMBER, result.blocks[0].flags)


# ─────────────────────────────────────────────────────────────────────────────
# Test 3: Suffix ambiguity flagging
# ─────────────────────────────────────────────────────────────────────────────

class TestSuffixAmbiguity(unittest.TestCase):
    def test_ambiguous_when_both_suffixes_in_whitelist(self):
        """
        Whitelist has 12345-1 and 12345-2 but NOT bare 12345.
        A page showing only 12345 → AMBIGUOUS_SUFFIX.
        """
        whitelist = ["12345-1", "12345-2"]
        pages = [make_page(1, value="12345")]
        result = group_detections(pages, whitelist)
        self.assertIsNone(result.blocks[0].ticket)
        self.assertIn(FLAG_AMBIGUOUS_SUFFIX, result.blocks[0].flags)

    def test_no_ambiguity_when_only_one_suffix(self):
        """Whitelist has 12345-1 only. Page shows 12345-1 → resolves cleanly."""
        whitelist = ["12345-1"]
        pages = [make_page(1, value="12345-1")]
        result = group_detections(pages, whitelist)
        self.assertEqual(result.blocks[0].ticket, "12345-1")
        self.assertNotIn(FLAG_AMBIGUOUS_SUFFIX, result.blocks[0].flags)

    def test_base_in_whitelist_no_ambiguity(self):
        """Whitelist has bare 12345 only. Page shows 12345 → resolves to 12345."""
        whitelist = ["12345"]
        pages = [make_page(1, value="12345")]
        result = group_detections(pages, whitelist)
        self.assertEqual(result.blocks[0].ticket, "12345")
        self.assertNotIn(FLAG_AMBIGUOUS_SUFFIX, result.blocks[0].flags)


# ─────────────────────────────────────────────────────────────────────────────
# Test 4: Inheritance across photo pages
# ─────────────────────────────────────────────────────────────────────────────

class TestInheritance(unittest.TestCase):
    def test_photo_pages_inherit_from_previous(self):
        """
        Pages 2 and 3 have no detected number (photo pages).
        They should inherit ticket from page 1.
        """
        whitelist = ["253983"]
        pages = make_pages("253983", None, None)
        result = group_detections(pages, whitelist)
        self.assertEqual(len(result.blocks), 1)
        self.assertEqual(result.blocks[0].ticket, "253983")
        self.assertEqual(result.blocks[0].pages, [1, 2, 3])

    def test_inheritance_chain(self):
        """A long run of photo pages all inherit from the first detected page."""
        whitelist = ["253983", "301532"]
        pages = make_pages("253983", None, None, None, "301532", None)
        result = group_detections(pages, whitelist)
        self.assertEqual(len(result.blocks), 2)
        self.assertEqual(result.blocks[0].ticket, "253983")
        self.assertEqual(result.blocks[0].pages, [1, 2, 3, 4])
        self.assertEqual(result.blocks[1].ticket, "301532")
        self.assertEqual(result.blocks[1].pages, [5, 6])


# ─────────────────────────────────────────────────────────────────────────────
# Test 5: Orphan leading pages
# ─────────────────────────────────────────────────────────────────────────────

class TestOrphanLeadingPages(unittest.TestCase):
    def test_orphan_before_first_detection(self):
        """Pages before the first detected number → ORPHAN_LEADING_PAGES."""
        whitelist = ["253983"]
        pages = make_pages(None, None, "253983")
        result = group_detections(pages, whitelist)
        # Should have an orphan block (pages 1-2) and a resolved block (page 3)
        self.assertEqual(len(result.blocks), 2)
        orphan_block = result.blocks[0]
        self.assertIsNone(orphan_block.ticket)
        self.assertIn(FLAG_ORPHAN_LEADING_PAGES, orphan_block.flags)
        self.assertEqual(orphan_block.pages, [1, 2])
        self.assertEqual(result.blocks[1].ticket, "253983")
        self.assertEqual(result.blocks[1].pages, [3])

    def test_no_orphan_when_first_page_detected(self):
        """No orphan if the first page has a detected number."""
        whitelist = ["253983"]
        pages = make_pages("253983", None)
        result = group_detections(pages, whitelist)
        self.assertEqual(len(result.blocks), 1)
        self.assertNotIn(FLAG_ORPHAN_LEADING_PAGES, result.blocks[0].flags)


# ─────────────────────────────────────────────────────────────────────────────
# Test 6: Non-contiguous merge + flag
# ─────────────────────────────────────────────────────────────────────────────

class TestNonContiguous(unittest.TestCase):
    def test_non_contiguous_pages_merged_and_flagged(self):
        """
        Ticket 253983 appears on pages 1-2, then 301532 on page 3, then 253983 again on page 4.
        The two 253983 blocks ARE separated by a different named ticket (301532) →
        must be merged and flagged NON_CONTIGUOUS.
        """
        whitelist = ["253983", "301532"]
        pages = make_pages("253983", "253983", "301532", "253983")
        result = group_detections(pages, whitelist)
        # Should have 2 blocks: merged 253983 (pages 1,2,4) and 301532 (page 3)
        tickets = {b.ticket: b for b in result.blocks}
        self.assertIn("253983", tickets)
        self.assertIn("301532", tickets)
        merged = tickets["253983"]
        self.assertEqual(sorted(merged.pages), [1, 2, 4])
        self.assertIn(FLAG_NON_CONTIGUOUS, merged.flags)

    def test_contiguous_no_flag(self):
        """Contiguous pages for the same ticket must NOT be flagged NON_CONTIGUOUS."""
        whitelist = ["253983"]
        pages = make_pages("253983", "253983", "253983")
        result = group_detections(pages, whitelist)
        self.assertEqual(len(result.blocks), 1)
        self.assertNotIn(FLAG_NON_CONTIGUOUS, result.blocks[0].flags)

    def test_trailing_photo_pages_no_non_contiguous(self):
        """
        Pages 1-3 detected as ticket A, pages 4-6 have no detection (photo pages),
        page 7 detected as ticket B.
        Pages 4-6 inherit from page 3 (ticket A) → block A is pages 1-6, block B is page 7.
        The two A-runs are NOT separated by a different named ticket (they are separated
        by inherited pages, which are already part of block A after inheritance).
        NON_CONTIGUOUS must NOT fire.
        """
        whitelist = ["253983", "301532"]
        pages = [
            make_page(1, value="253983"),
            make_page(2, value="253983"),
            make_page(3, value="253983"),
            make_page(4),  # no detection → inherits 253983
            make_page(5),  # no detection → inherits 253983
            make_page(6),  # no detection → inherits 253983
            make_page(7, value="301532"),
        ]
        result = group_detections(pages, whitelist)
        # Block A should be pages 1-6, block B should be page 7
        a_block = next(b for b in result.blocks if b.ticket == "253983")
        b_block = next(b for b in result.blocks if b.ticket == "301532")
        self.assertEqual(a_block.pages, [1, 2, 3, 4, 5, 6])
        self.assertEqual(b_block.pages, [7])
        self.assertNotIn(FLAG_NON_CONTIGUOUS, a_block.flags)


# ─────────────────────────────────────────────────────────────────────────────
# Test 7: Edit-distance-1 whitelist match, unique candidate
# ─────────────────────────────────────────────────────────────────────────────

class TestEditDistance1Unique(unittest.TestCase):
    def test_edit_distance_1_resolves_to_unique_match(self):
        """
        Detected 243983 (edit-distance 1 from 253983, unique in whitelist) →
        auto-resolved to 253983 with FUZZY_RESOLVED soft flag.
        """
        whitelist = ["301532", "257535", "253983", "258066", "257086"]
        pages = [make_page(1, value="243983", confidence=0.92)]
        result = group_detections(pages, whitelist)
        self.assertEqual(result.blocks[0].ticket, "253983")
        self.assertNotIn(FLAG_UNMATCHED_NUMBER, result.blocks[0].flags)
        self.assertIn(FLAG_FUZZY_RESOLVED, result.blocks[0].flags)

    def test_edit_distance_1_low_confidence_flagged(self):
        """
        A fuzzy-matched page with confidence < 0.7 → LOW_CONFIDENCE flag.
        FUZZY_RESOLVED is also present (separate concern from confidence).
        """
        whitelist = ["253983"]
        pages = [make_page(1, value="243983", confidence=0.65)]
        result = group_detections(pages, whitelist)
        self.assertEqual(result.blocks[0].ticket, "253983")
        self.assertIn(FLAG_LOW_CONFIDENCE, result.blocks[0].flags)
        self.assertIn(FLAG_FUZZY_RESOLVED, result.blocks[0].flags)

    def test_exact_match_no_fuzzy_resolved_flag(self):
        """An exact whitelist match must NOT carry FUZZY_RESOLVED."""
        whitelist = ["253983"]
        pages = [make_page(1, value="253983", confidence=0.95)]
        result = group_detections(pages, whitelist)
        self.assertEqual(result.blocks[0].ticket, "253983")
        self.assertNotIn(FLAG_FUZZY_RESOLVED, result.blocks[0].flags)


# ─────────────────────────────────────────────────────────────────────────────
# Test 8: Edit-distance-1 with two candidates → flag
# ─────────────────────────────────────────────────────────────────────────────

class TestEditDistance1TwoCandidates(unittest.TestCase):
    def test_edit_distance_1_two_matches_flags_ambiguous(self):
        """
        A detected value that is edit-distance-1 from TWO whitelist entries →
        must NOT auto-assign; must flag UNMATCHED_NUMBER (or AMBIGUOUS_SUFFIX).
        """
        # 123456 is edit-distance 1 from both 123556 and 123466
        whitelist = ["123556", "123466"]
        # 123456 → 123556: sub pos3 (4→5) = distance 1
        # 123456 → 123466: sub pos4 (5→6) = distance 1
        pages = [make_page(1, value="123456")]
        result = group_detections(pages, whitelist)
        # Must not auto-assign to either
        self.assertIsNone(result.blocks[0].ticket)
        # Must have a flag indicating the ambiguity
        block_flags = result.blocks[0].flags
        self.assertTrue(
            FLAG_UNMATCHED_NUMBER in block_flags or FLAG_AMBIGUOUS_SUFFIX in block_flags,
            f"Expected UNMATCHED_NUMBER or AMBIGUOUS_SUFFIX, got: {block_flags}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Test 9 (Gate 1 addition): Edit-distance-2 between same-ticket neighbors
# ─────────────────────────────────────────────────────────────────────────────

class TestEditDistance2InheritedUnmatched(unittest.TestCase):
    def test_edit_distance_2_inherits_when_prev_block_matches(self):
        """
        Decision 8 amendment: page 7 (247983, edit-dist 2 from 253983).
        Previous block is 253983 → Step 2: INHERITED_UNMATCHED (soft flag).
        Page 7 joins the 253983 block, does NOT hard-flag.
        """
        whitelist = ["301532", "257535", "253983", "258066", "257086"]
        pages = [
            make_page(1, value="253983"),
            make_page(2, value="253983"),
            make_page(3, value="253983"),
            make_page(4, value="253983"),
            make_page(5, value="253983"),
            make_page(6, value="253983"),
            make_page(7, value="247983"),  # edit-dist 2 from 253983 → INHERITED_UNMATCHED
            make_page(8, value="253983"),
            make_page(9, value="253983"),
            make_page(10, value="253983"),
        ]
        result = group_detections(pages, whitelist)

        # Page 7 must be in the 253983 block
        b = next((b for b in result.blocks if b.ticket == "253983"), None)
        self.assertIsNotNone(b, "253983 block must exist")
        self.assertIn(7, b.pages, "Page 7 must be in 253983 block via INHERITED_UNMATCHED")
        self.assertIn(FLAG_INHERITED_UNMATCHED, b.flags)
        self.assertNotIn(FLAG_UNMATCHED_NUMBER, b.flags)
        # Soft flag — must not block Confirm
        self.assertNotIn(FLAG_INHERITED_UNMATCHED, HARD_FLAGS)

    def test_edit_distance_2_hard_flags_when_prev_block_differs(self):
        """
        Page 2 (247983, edit-dist 2 from 253983) but previous block is 301532.
        Edit-dist(247983, 301532) >> 2 → Step 3: UNMATCHED_NUMBER hard flag.
        """
        whitelist = ["253983", "301532", "257535"]
        pages = [
            make_page(1, value="301532"),  # previous block is 301532
            make_page(2, value="247983"),  # edit-dist to 301532 >> 2 → hard flag
            make_page(3, value="253983"),
        ]
        result = group_detections(pages, whitelist)
        p2_block = next((b for b in result.blocks if 2 in b.pages), None)
        self.assertIsNotNone(p2_block)
        # Must be unresolved with hard UNMATCHED_NUMBER
        self.assertIsNone(p2_block.ticket)
        self.assertIn(FLAG_UNMATCHED_NUMBER, p2_block.flags)

    def test_consecutive_unmatched_different_raw_values_produce_separate_blocks(self):
        """
        Issue 1 fix: consecutive pages with different raw UNMATCHED values
        must produce two separately assignable blocks, not one merged block.
        """
        whitelist = ["111111", "222222"]  # neither 999991 nor 888881 match
        pages = [
            make_page(1, value="999991"),  # unmatched, raw=999991
            make_page(2, value="888881"),  # unmatched, raw=888881 (different)
        ]
        result = group_detections(pages, whitelist)
        # Must produce 2 separate blocks (one per raw value)
        unmatched_blocks = [b for b in result.blocks if b.ticket is None]
        self.assertEqual(len(unmatched_blocks), 2,
            f"Expected 2 separate unmatched blocks, got {len(unmatched_blocks)}: "
            f"{[(b.unmatched_raw, b.pages) for b in unmatched_blocks]}")
        raw_values = {b.unmatched_raw[0] for b in unmatched_blocks if b.unmatched_raw}
        self.assertIn("999991", raw_values)
        self.assertIn("888881", raw_values)


# ─────────────────────────────────────────────────────────────────────────────
# Test 10: Missing ticket completeness check
# ─────────────────────────────────────────────────────────────────────────────

class TestMissingTicket(unittest.TestCase):
    def test_missing_ticket_reported(self):
        """Whitelist tickets with zero pages must appear in missing_tickets."""
        whitelist = ["253983", "301532", "257535"]
        pages = make_pages("253983")  # only 253983 present
        result = group_detections(pages, whitelist)
        self.assertIn("301532", result.missing_tickets)
        self.assertIn("257535", result.missing_tickets)
        self.assertNotIn("253983", result.missing_tickets)

    def test_no_missing_when_all_present(self):
        whitelist = ["253983", "301532"]
        pages = make_pages("253983", "301532")
        result = group_detections(pages, whitelist)
        self.assertEqual(result.missing_tickets, [])


# ─────────────────────────────────────────────────────────────────────────────
# Test 11: Low confidence flag
# ─────────────────────────────────────────────────────────────────────────────

class TestLowConfidence(unittest.TestCase):
    def test_low_confidence_flagged(self):
        whitelist = ["253983"]
        pages = [make_page(1, value="253983", confidence=0.65)]
        result = group_detections(pages, whitelist)
        self.assertIn(FLAG_LOW_CONFIDENCE, result.blocks[0].flags)

    def test_high_confidence_not_flagged(self):
        whitelist = ["253983"]
        pages = [make_page(1, value="253983", confidence=0.95)]
        result = group_detections(pages, whitelist)
        self.assertNotIn(FLAG_LOW_CONFIDENCE, result.blocks[0].flags)

    def test_exactly_07_not_flagged(self):
        """Confidence exactly 0.7 is NOT below 0.7 — should not flag."""
        whitelist = ["253983"]
        pages = [make_page(1, value="253983", confidence=0.70)]
        result = group_detections(pages, whitelist)
        self.assertNotIn(FLAG_LOW_CONFIDENCE, result.blocks[0].flags)


# ─────────────────────────────────────────────────────────────────────────────
# Test 12: Integration — test file detection results → 5 correct blocks
# ─────────────────────────────────────────────────────────────────────────────

class TestIntegrationTestFile(unittest.TestCase):
    """
    Simulates the detection results from the 16-page testingfile.pdf
    (as confirmed at Gate 1) and verifies the grouping engine produces
    exactly the 5 correct blocks with the correct page ranges.

    Page 7 uses the actual model output (247983 — edit-distance 2, UNMATCHED).
    Page 10 uses the actual model output (243983 — edit-distance 1, auto-resolved).
    All other pages use the correct values as detected.
    """
    WHITELIST = ["301532", "257535", "253983", "258066", "257086"]

    def _make_detection_results(self):
        return [
            make_page(1,  "301532", source="printed",     confidence=0.98),
            make_page(2,  "301532", source="printed",     confidence=0.99),
            make_page(3,  "301532", source="handwritten", confidence=0.97),
            make_page(4,  "257535", source="sticker",     confidence=0.98),
            make_page(5,  "253983", source="handwritten", confidence=0.90),
            make_page(6,  "253983", source="handwritten", confidence=0.78),
            make_page(7,  "247983", source="handwritten", confidence=0.73),  # misread
            make_page(8,  "253983", source="handwritten", confidence=0.93),
            make_page(9,  "253983", source="handwritten", confidence=0.90),
            make_page(10, "243983", source="handwritten", confidence=0.92),  # fuzzy→253983
            make_page(11, "258066", source="sticker",     confidence=0.98),
            make_page(12, "258066", source="sticker",     confidence=0.97),
            make_page(13, "258066", source="sticker",     confidence=0.96),
            make_page(14, "257086", source="printed",     confidence=0.99),
            make_page(15, "257086", source="printed",     confidence=0.99),
            make_page(16, "257086", source="sticker",     confidence=0.92),
        ]

    def test_correct_number_of_blocks(self):
        """After human resolves page 7, we should have exactly 5 blocks (6 before resolution)."""
        pages = self._make_detection_results()
        result = group_detections(pages, self.WHITELIST)
        # Before human resolution: page 7 is UNMATCHED → separate block
        # So we expect 6 blocks: 301532, 257535, 253983(5-6), UNMATCHED(7), 253983(8-10), 258066, 257086
        # But 253983 blocks are non-contiguous → they get merged into one + NON_CONTIGUOUS flag
        # So: 301532, 257535, merged-253983(5-6+8-10), UNMATCHED(7), 258066, 257086
        # Wait — the non-contiguous merge happens after block building.
        # The UNMATCHED block for page 7 has ticket=None, so it won't merge with 253983.
        # Let's count: 301532(1-3), 257535(4), 253983(5-6), UNMATCHED(7), 253983(8-10), 258066(11-13), 257086(14-16)
        # After non-contiguous merge: 253983 appears in blocks 3 and 5 → merged → 5 named + 1 unmatched = 6 blocks
        # With the INHERITED_UNMATCHED amendment (Decision 8):
        # Page 7 (247983, edit-dist 2 from 253983) now inherits into 253983 block.
        # So all 5 whitelist tickets are assigned, 0 unmatched blocks.
        ticket_blocks = [b for b in result.blocks if b.ticket is not None]
        unmatched_blocks = [b for b in result.blocks if b.ticket is None]
        self.assertEqual(len(ticket_blocks), 5, f"Expected 5 named blocks, got: {[(b.ticket, b.pages) for b in ticket_blocks]}")
        self.assertEqual(len(unmatched_blocks), 0, f"Expected 0 unmatched blocks (page 7 now INHERITED_UNMATCHED), got: {unmatched_blocks}")

    def test_301532_pages_1_to_3(self):
        pages = self._make_detection_results()
        result = group_detections(pages, self.WHITELIST)
        b = next(b for b in result.blocks if b.ticket == "301532")
        self.assertEqual(b.pages, [1, 2, 3])

    def test_257535_page_4(self):
        pages = self._make_detection_results()
        result = group_detections(pages, self.WHITELIST)
        b = next(b for b in result.blocks if b.ticket == "257535")
        self.assertEqual(b.pages, [4])

    def test_253983_pages_5_to_10_merged(self):
        """
        Pages 5-6 detected as 253983; page 7 (247983) INHERITED_UNMATCHED into 253983;
        pages 8-10 detected as 253983 (page 10 via fuzzy match).
        All 6 pages (5-10) must be in one block with no NON_CONTIGUOUS flag.
        """
        pages = self._make_detection_results()
        result = group_detections(pages, self.WHITELIST)
        b = next(b for b in result.blocks if b.ticket == "253983")
        self.assertEqual(sorted(b.pages), [5, 6, 7, 8, 9, 10])
        self.assertNotIn(FLAG_NON_CONTIGUOUS, b.flags)

    def test_258066_pages_11_to_13(self):
        pages = self._make_detection_results()
        result = group_detections(pages, self.WHITELIST)
        b = next(b for b in result.blocks if b.ticket == "258066")
        self.assertEqual(b.pages, [11, 12, 13])

    def test_257086_pages_14_to_16(self):
        pages = self._make_detection_results()
        result = group_detections(pages, self.WHITELIST)
        b = next(b for b in result.blocks if b.ticket == "257086")
        self.assertEqual(b.pages, [14, 15, 16])

    def test_page7_inherited_unmatched(self):
        """
        Page 7 (247983, edit-distance 2 from 253983).
        Previous block is 253983. Edit-distance ≤ 2 → Step 2: INHERITED_UNMATCHED.
        Page 7 is now part of the 253983 block with INHERITED_UNMATCHED soft flag.
        This is a soft flag — it does NOT block Confirm.
        (Amendment to LOCKED rule 5, Decision 8.)
        """
        pages = self._make_detection_results()
        result = group_detections(pages, self.WHITELIST)
        # Page 7 must now be in the 253983 block (inherited)
        b_253983 = next((b for b in result.blocks if b.ticket == "253983"), None)
        self.assertIsNotNone(b_253983, "253983 block must exist")
        self.assertIn(7, b_253983.pages, "Page 7 must be in 253983 block via INHERITED_UNMATCHED")
        # Must carry INHERITED_UNMATCHED soft flag
        self.assertIn(FLAG_INHERITED_UNMATCHED, b_253983.flags)
        # Must NOT carry UNMATCHED_NUMBER hard flag
        self.assertNotIn(FLAG_UNMATCHED_NUMBER, b_253983.flags)
        # Must NOT be Confirm-blocking
        self.assertNotIn(FLAG_INHERITED_UNMATCHED, HARD_FLAGS)

    def test_no_missing_tickets(self):
        """After grouping, no whitelist ticket should be in missing_tickets
        (all 5 are assigned, even though page 7 is unmatched)."""
        pages = self._make_detection_results()
        result = group_detections(pages, self.WHITELIST)
        # 253983 IS assigned (to the merged block), so it's not missing
        self.assertNotIn("253983", result.missing_tickets)
        self.assertEqual(result.missing_tickets, [])

    def test_page10_fuzzy_resolved_to_253983(self):
        """Page 10 (243983, edit-distance 1) must resolve to 253983, not be flagged UNMATCHED,
        and the 253983 block must carry FUZZY_RESOLVED soft flag."""
        pages = self._make_detection_results()
        result = group_detections(pages, self.WHITELIST)
        # Page 10 should be in the 253983 block
        b = next(b for b in result.blocks if b.ticket == "253983")
        self.assertIn(10, b.pages)
        self.assertIn(FLAG_FUZZY_RESOLVED, b.flags)


# ─────────────────────────────────────────────────────────────────────────────
# Test 13: Duplicate cover pages (same number, same block — harmless)
# ─────────────────────────────────────────────────────────────────────────────

class TestDuplicateCoverPages(unittest.TestCase):
    def test_duplicate_cover_same_block(self):
        """Pages 1 and 2 both detect 301532 → one block, not two."""
        whitelist = ["301532"]
        pages = make_pages("301532", "301532", "301532")
        result = group_detections(pages, whitelist)
        self.assertEqual(len(result.blocks), 1)
        self.assertEqual(result.blocks[0].ticket, "301532")
        self.assertEqual(result.blocks[0].pages, [1, 2, 3])
        self.assertNotIn(FLAG_NON_CONTIGUOUS, result.blocks[0].flags)


# ─────────────────────────────────────────────────────────────────────────────
# Test 14: Unmatched values reported in side panel
# ─────────────────────────────────────────────────────────────────────────────

class TestUnmatchedValuesSidePanel(unittest.TestCase):
    def test_unmatched_values_listed(self):
        """All detected values that matched nothing must appear in result.unmatched_values."""
        whitelist = ["253983"]
        pages = [
            make_page(1, value="253983"),
            make_page(2, value="999999"),  # not in whitelist, not close
        ]
        result = group_detections(pages, whitelist)
        self.assertIn("999999", result.unmatched_values)


# ─────────────────────────────────────────────────────────────────────────────
# Test 15: Regression fixture 2 — SKM_C250i26070816150.pdf (16 pages)
# Ground truth confirmed by owner.
# Whitelist: 300291, 300871, 300588, 298404, 299198, 301053
# Expected: 6 blocks, no hard flags, one FUZZY_RESOLVED on 299198 block,
#           CID 475545 nowhere assigned.
# ─────────────────────────────────────────────────────────────────────────────

class TestRegressionFixture2(unittest.TestCase):
    """
    Regression fixture for SKM_C250i26070816150.pdf (16 pages).
    Detection results as returned by the raw model (no code changes).
    Ground truth confirmed by owner at Gate 2 review.

    Page map:
      300291 = page 1 (sticker, conf=0.78)
      300871 = page 2 (sticker, conf=0.80)
      300588 = pages 3-11 (sticker+handwritten; pages 8-11 inherit from page 7)
      298404 = page 12 (sticker, conf=0.95)
      299198 = page 13 (sticker, conf=0.74; raw detection was 299188 → fuzzy-resolved)
      301053 = pages 14-16 (printed; pages 15-16 inherit from page 14)

    Acceptance criteria:
      - 6 named blocks, 0 unmatched blocks
      - No hard flags (UNMATCHED_NUMBER, AMBIGUOUS_SUFFIX, ORPHAN_LEADING_PAGES)
      - 299198 block carries FUZZY_RESOLVED soft flag
      - Customer ID 475545 appears nowhere in any block's ticket or unmatched_raw
    """

    WHITELIST = ["300291", "300871", "300588", "298404", "299198", "301053"]

    def _make_detection_results(self):
        """Raw detection results as returned by the model on this file."""
        return [
            make_page(1,  "300291", source="sticker",     confidence=0.78),
            make_page(2,  "300871", source="sticker",     confidence=0.80),
            make_page(3,  "300588", source="sticker",     confidence=0.93),
            make_page(4,  "300588", source="handwritten", confidence=0.92),
            make_page(5,  "300588", source="handwritten", confidence=0.91),
            make_page(6,  "300588", source="handwritten", confidence=0.90),
            make_page(7,  "300588", source="handwritten", confidence=0.86),
            make_page(8),   # no detection → inherits 300588
            make_page(9),   # no detection → inherits 300588
            make_page(10),  # no detection → inherits 300588
            make_page(11),  # no detection → inherits 300588
            make_page(12, "298404", source="sticker",     confidence=0.95),
            make_page(13, "299188", source="sticker",     confidence=0.74),  # fuzzy → 299198
            make_page(14, "301053", source="printed",     confidence=0.74),
            make_page(15),  # no detection → inherits 301053
            make_page(16),  # no detection → inherits 301053
        ]

    def test_exactly_6_named_blocks(self):
        """All 6 whitelist tickets must be assigned; 0 unmatched blocks."""
        pages = self._make_detection_results()
        result = group_detections(pages, self.WHITELIST)
        named = [b for b in result.blocks if b.ticket is not None]
        unmatched = [b for b in result.blocks if b.ticket is None]
        self.assertEqual(len(named), 6,
            f"Expected 6 named blocks, got: {[(b.ticket, b.pages) for b in named]}")
        self.assertEqual(len(unmatched), 0,
            f"Expected 0 unmatched blocks, got: {unmatched}")

    def test_no_missing_tickets(self):
        """All 6 whitelist tickets must be present."""
        pages = self._make_detection_results()
        result = group_detections(pages, self.WHITELIST)
        self.assertEqual(result.missing_tickets, [])

    def test_300291_page_1(self):
        pages = self._make_detection_results()
        result = group_detections(pages, self.WHITELIST)
        b = next(b for b in result.blocks if b.ticket == "300291")
        self.assertEqual(b.pages, [1])

    def test_300871_page_2(self):
        pages = self._make_detection_results()
        result = group_detections(pages, self.WHITELIST)
        b = next(b for b in result.blocks if b.ticket == "300871")
        self.assertEqual(b.pages, [2])

    def test_300588_pages_3_to_11(self):
        """Pages 3-7 detected; pages 8-11 inherit → one block, no NON_CONTIGUOUS."""
        pages = self._make_detection_results()
        result = group_detections(pages, self.WHITELIST)
        b = next(b for b in result.blocks if b.ticket == "300588")
        self.assertEqual(b.pages, list(range(3, 12)))
        self.assertNotIn(FLAG_NON_CONTIGUOUS, b.flags)

    def test_298404_page_12(self):
        pages = self._make_detection_results()
        result = group_detections(pages, self.WHITELIST)
        b = next(b for b in result.blocks if b.ticket == "298404")
        self.assertEqual(b.pages, [12])

    def test_299198_page_13_fuzzy_resolved(self):
        """Raw 299188 → fuzzy-resolved to 299198; block must carry FUZZY_RESOLVED."""
        pages = self._make_detection_results()
        result = group_detections(pages, self.WHITELIST)
        b = next(b for b in result.blocks if b.ticket == "299198")
        self.assertEqual(b.pages, [13])
        self.assertIn(FLAG_FUZZY_RESOLVED, b.flags)
        self.assertNotIn(FLAG_UNMATCHED_NUMBER, b.flags)

    def test_301053_pages_14_to_16(self):
        """Page 14 detected; pages 15-16 inherit → one block."""
        pages = self._make_detection_results()
        result = group_detections(pages, self.WHITELIST)
        b = next(b for b in result.blocks if b.ticket == "301053")
        self.assertEqual(b.pages, [14, 15, 16])

    def test_no_hard_flags(self):
        """No block must carry any hard flag."""
        pages = self._make_detection_results()
        result = group_detections(pages, self.WHITELIST)
        hard_flag_set = {FLAG_UNMATCHED_NUMBER, FLAG_AMBIGUOUS_SUFFIX, FLAG_ORPHAN_LEADING_PAGES}
        for b in result.blocks:
            for f in b.flags:
                self.assertNotIn(f, hard_flag_set,
                    f"Block {b.ticket} (pages {b.pages}) has unexpected hard flag: {f}")

    def test_customer_id_475545_not_assigned(self):
        """CID 475545 must not appear as any block's ticket or in any unmatched_raw list."""
        pages = self._make_detection_results()
        result = group_detections(pages, self.WHITELIST)
        for b in result.blocks:
            self.assertNotEqual(b.ticket, "475545",
                f"CID 475545 must not be assigned as a ticket")
            self.assertNotIn("475545", b.unmatched_raw,
                f"CID 475545 must not appear in unmatched_raw")


# ─────────────────────────────────────────────────────────────────────────────
# Test 16: SECOND_PASS soft flag
# ─────────────────────────────────────────────────────────────────────────────

class TestSecondPassFlag(unittest.TestCase):
    def test_second_pass_flag_attached_when_candidate_is_second_pass(self):
        """A candidate with second_pass=True must cause SECOND_PASS flag on the block."""
        whitelist = ["300574", "300600"]
        pages = [
            make_page(1, value="300574", source="sticker", confidence=0.85, second_pass=True),
            make_page(2, value="300600", source="sticker", confidence=0.95, second_pass=False),
        ]
        result = group_detections(pages, whitelist)
        b1 = next(b for b in result.blocks if b.ticket == "300574")
        b2 = next(b for b in result.blocks if b.ticket == "300600")
        self.assertIn(FLAG_SECOND_PASS, b1.flags,
            "Block with second_pass candidate must carry SECOND_PASS flag")
        self.assertNotIn(FLAG_SECOND_PASS, b2.flags,
            "Block with normal candidate must NOT carry SECOND_PASS flag")

    def test_second_pass_flag_not_attached_for_normal_detection(self):
        """A normal first-pass candidate must NOT cause SECOND_PASS flag."""
        whitelist = ["300574"]
        pages = [make_page(1, value="300574", confidence=0.93, second_pass=False)]
        result = group_detections(pages, whitelist)
        self.assertNotIn(FLAG_SECOND_PASS, result.blocks[0].flags)

    def test_second_pass_flag_is_soft_not_hard(self):
        """SECOND_PASS must not be in HARD_FLAGS — it must not block Confirm."""
        from grouping import HARD_FLAGS
        self.assertNotIn(FLAG_SECOND_PASS, HARD_FLAGS)


# ─────────────────────────────────────────────────────────────────────────────
# Test 17: Regression fixture 4 — SKM_C250i26070816530.pdf (18 pages)
# Replaces fixture #3 (same content + suffix case at end).
# Ground truth confirmed by owner.
# Whitelist: 300574, 300600, 300573, 253027-1
# Expected: 4 blocks, zero hard flags, page 1 may carry SECOND_PASS soft flag.
# ─────────────────────────────────────────────────────────────────────────────

class TestRegressionFixture4(unittest.TestCase):
    """
    Regression fixture for SKM_C250i26070816530.pdf (18 pages).
    Ground truth confirmed by owner.

    Page map:
      300574  = pages 1-2   (p1: sticker on photo page — may be SECOND_PASS; p2: handwritten)
      300600  = pages 3-13  (p3: sticker; pages 4-13 inherit)
      300573  = pages 14-15 (p14: sticker; p15 inherits)
      253027-1 = pages 16-18 (p16: printed base+handwritten suffix; p17: bare base → single-suffix-variant;
                               p18: no number → inherits)

    Acceptance criteria:
      - 4 named blocks, 0 unmatched blocks
      - No hard flags
      - 253027-1 block covers pages 16-18
      - 253027-2 must not appear anywhere (annotation trap)
      - 107408 (Vendor#) and 27823 (Customer#) must not appear anywhere
    """

    WHITELIST = ["300574", "300600", "300573", "253027-1"]

    def _make_detection_results_normal_pass(self):
        """First-pass detection: page 1 missed (sticker on photo page at angle)."""
        return [
            make_page(1),   # first-pass miss → ORPHAN without second pass
            make_page(2,  "300574", source="handwritten", confidence=0.92),
            make_page(3,  "300600", source="sticker",     confidence=0.90),
            make_page(4),
            make_page(5),
            make_page(6),
            make_page(7),
            make_page(8),
            make_page(9),
            make_page(10),
            make_page(11),
            make_page(12),
            make_page(13),
            make_page(14, "300573", source="sticker",     confidence=0.90),
            make_page(15),
            make_page(16, "253027-1", source="printed",   confidence=0.97),
            make_page(17, "253027",   source="handwritten", confidence=0.90),
            make_page(18),
        ]

    def _make_detection_results_with_second_pass(self):
        """Second-pass recovers page 1 via whitelist-context query."""
        pages = self._make_detection_results_normal_pass()
        # Replace page 1 with a second-pass detection
        pages[0] = make_page(1, "300574", source="sticker", confidence=0.85, second_pass=True)
        return pages

    # ── Scenario A: second pass recovers page 1 ──

    def test_4_blocks_with_second_pass(self):
        """With second pass, all 4 tickets assigned, 0 unmatched."""
        pages = self._make_detection_results_with_second_pass()
        result = group_detections(pages, self.WHITELIST)
        named = [b for b in result.blocks if b.ticket is not None]
        unmatched = [b for b in result.blocks if b.ticket is None]
        self.assertEqual(len(named), 4,
            f"Expected 4 named blocks, got: {[(b.ticket, b.pages) for b in named]}")
        self.assertEqual(len(unmatched), 0)

    def test_300574_pages_1_2_with_second_pass(self):
        pages = self._make_detection_results_with_second_pass()
        result = group_detections(pages, self.WHITELIST)
        b = next(b for b in result.blocks if b.ticket == "300574")
        self.assertEqual(b.pages, [1, 2])
        self.assertIn(FLAG_SECOND_PASS, b.flags)

    def test_no_hard_flags_with_second_pass(self):
        pages = self._make_detection_results_with_second_pass()
        result = group_detections(pages, self.WHITELIST)
        from grouping import HARD_FLAGS
        for b in result.blocks:
            for f in b.flags:
                self.assertNotIn(f, HARD_FLAGS,
                    f"Block {b.ticket} (pages {b.pages}) has unexpected hard flag: {f}")

    # ── Scenario B: second pass also misses page 1 (fallback) ──

    def test_orphan_fallback_when_second_pass_misses(self):
        """If second pass also misses page 1, it becomes ORPHAN with 300574 pre-selected."""
        pages = self._make_detection_results_normal_pass()
        result = group_detections(pages, self.WHITELIST)
        orphan_blocks = [b for b in result.blocks if b.ticket is None]
        self.assertEqual(len(orphan_blocks), 1)
        self.assertIn(FLAG_ORPHAN_LEADING_PAGES, orphan_blocks[0].flags)
        self.assertEqual(orphan_blocks[0].pages, [1])
        # Neighbor suggestion must pre-select 300574 (the next named block)
        self.assertEqual(orphan_blocks[0].neighbor_suggestion, "300574")

    # ── Shared assertions (both scenarios) ──

    def test_300600_pages_3_to_13(self):
        """Pages 3-13: sticker on p3, 10-page inheritance chain, no NON_CONTIGUOUS."""
        pages = self._make_detection_results_with_second_pass()
        result = group_detections(pages, self.WHITELIST)
        b = next(b for b in result.blocks if b.ticket == "300600")
        self.assertEqual(b.pages, list(range(3, 14)))
        self.assertNotIn(FLAG_NON_CONTIGUOUS, b.flags)

    def test_300573_pages_14_15(self):
        pages = self._make_detection_results_with_second_pass()
        result = group_detections(pages, self.WHITELIST)
        b = next(b for b in result.blocks if b.ticket == "300573")
        self.assertEqual(b.pages, [14, 15])

    def test_253027_1_pages_16_to_18_suffix_wins(self):
        """
        Page 16: printed '253027-1' (suffix-wins / direct suffixed detection).
        Page 17: bare '253027' → single-suffix-variant rule → resolves to 253027-1.
        Page 18: no detection → inherits 253027-1.
        Block must cover pages 16-18.
        """
        pages = self._make_detection_results_with_second_pass()
        result = group_detections(pages, self.WHITELIST)
        b = next(b for b in result.blocks if b.ticket == "253027-1")
        self.assertEqual(b.pages, [16, 17, 18])

    def test_253027_2_not_assigned(self):
        """The '-2 non TIB' annotation on page 16 must not produce a 253027-2 block."""
        pages = self._make_detection_results_with_second_pass()
        result = group_detections(pages, self.WHITELIST)
        for b in result.blocks:
            self.assertNotEqual(b.ticket, "253027-2")
            self.assertNotIn("253027-2", b.unmatched_raw)

    def test_decoy_numbers_not_assigned(self):
        """Vendor# 107408 and Customer# 27823 on page 16 must not appear anywhere."""
        pages = self._make_detection_results_with_second_pass()
        result = group_detections(pages, self.WHITELIST)
        for b in result.blocks:
            self.assertNotEqual(b.ticket, "107408")
            self.assertNotEqual(b.ticket, "27823")
            self.assertNotIn("107408", b.unmatched_raw)
            self.assertNotIn("27823", b.unmatched_raw)

    def test_no_missing_tickets(self):
        pages = self._make_detection_results_with_second_pass()
        result = group_detections(pages, self.WHITELIST)
        self.assertEqual(result.missing_tickets, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)


# ─────────────────────────────────────────────────────────────────────────────
# Test 18: Regression fixture 5 — SKM_C250i26070916020.pdf (17 pages)
# Ground truth confirmed by owner.
# Whitelist: 247799, 248256, 248258, 248259, 248260
# Expected: 5 clean blocks, soft flags only (INHERITED_UNMATCHED on handwritten
#           photo pages), no hard flags.
#
# Page map:
#   247799  = pages 1–4   (p1-2: printed covers; p3-4: handwritten photo pages)
#   248256  = pages 5–8   (p5-6: printed covers; p7-8: handwritten photo pages)
#   248258  = pages 9–11  (p9-10: printed covers; p11: handwritten photo page)
#   248259  = pages 12–14 (p12-13: printed covers; p14: handwritten photo page)
#   248260  = pages 15–17 (p15-16: printed covers; p17: handwritten photo page)
#
# Handwritten photo pages carry misread values (edit-dist ≤ 2 from correct ticket)
# → INHERITED_UNMATCHED soft flag (Decision 8 amendment).
# ─────────────────────────────────────────────────────────────────────────────

class TestRegressionFixture5(unittest.TestCase):
    """
    Regression fixture for SKM_C250i26070916020.pdf (17 pages).
    Ground truth confirmed by owner.
    """

    WHITELIST = ["247799", "248256", "248258", "248259", "248260"]

    def _make_detection_results(self):
        """
        Simulated detection results.
        Printed covers detected cleanly; handwritten photo pages return
        misread values that are edit-distance ≤ 2 from the correct ticket.
        """
        # Misread values for handwritten pages:
        # Each is edit-distance EXACTLY 2 from the correct ticket
        # (two digit substitutions) so they go through INHERITED_UNMATCHED path.
        # edit_dist(007799, 247799)=2, edit_dist(008256, 248256)=2, etc.
        return [
            # 247799 block
            make_page(1,  "247799", source="printed",     confidence=0.98),
            make_page(2,  "247799", source="printed",     confidence=0.97),
            make_page(3,  "007799", source="handwritten", confidence=0.72),  # edit-dist 2 from 247799 → INHERITED_UNMATCHED
            make_page(4,  "247799", source="handwritten", confidence=0.80),
            # 248256 block
            make_page(5,  "248256", source="printed",     confidence=0.99),
            make_page(6,  "248256", source="printed",     confidence=0.98),
            make_page(7,  "008256", source="handwritten", confidence=0.75),  # edit-dist 2 from 248256 → INHERITED_UNMATCHED
            make_page(8,  "248256", source="handwritten", confidence=0.82),
            # 248258 block
            make_page(9,  "248258", source="printed",     confidence=0.99),
            make_page(10, "248258", source="printed",     confidence=0.98),
            make_page(11, "008258", source="handwritten", confidence=0.74),  # edit-dist 2 from 248258 → INHERITED_UNMATCHED
            # 248259 block
            make_page(12, "248259", source="printed",     confidence=0.99),
            make_page(13, "248259", source="printed",     confidence=0.98),
            make_page(14, "008259", source="handwritten", confidence=0.75),  # edit-dist 2 from 248259 → INHERITED_UNMATCHED
            # 248260 block
            make_page(15, "248260", source="printed",     confidence=0.99),
            make_page(16, "248260", source="printed",     confidence=0.98),
            make_page(17, "008260", source="handwritten", confidence=0.74),  # edit-dist 2 from 248260 → INHERITED_UNMATCHED
        ]

    def test_exactly_5_named_blocks(self):
        """All 5 whitelist tickets must be assigned; 0 unmatched blocks."""
        pages = self._make_detection_results()
        result = group_detections(pages, self.WHITELIST)
        named = [b for b in result.blocks if b.ticket is not None]
        unmatched = [b for b in result.blocks if b.ticket is None]
        self.assertEqual(len(named), 5,
            f"Expected 5 named blocks, got: {[(b.ticket, b.pages) for b in named]}")
        self.assertEqual(len(unmatched), 0,
            f"Expected 0 unmatched blocks, got: {unmatched}")

    def test_no_hard_flags(self):
        """No block must carry any hard flag."""
        pages = self._make_detection_results()
        result = group_detections(pages, self.WHITELIST)
        for b in result.blocks:
            for f in b.flags:
                self.assertNotIn(f, HARD_FLAGS,
                    f"Block {b.ticket} (pages {b.pages}) has unexpected hard flag: {f}")

    def test_no_missing_tickets(self):
        pages = self._make_detection_results()
        result = group_detections(pages, self.WHITELIST)
        self.assertEqual(result.missing_tickets, [])

    def test_247799_pages_1_to_4(self):
        pages = self._make_detection_results()
        result = group_detections(pages, self.WHITELIST)
        b = next(b for b in result.blocks if b.ticket == "247799")
        self.assertEqual(sorted(b.pages), [1, 2, 3, 4])

    def test_248256_pages_5_to_8(self):
        pages = self._make_detection_results()
        result = group_detections(pages, self.WHITELIST)
        b = next(b for b in result.blocks if b.ticket == "248256")
        self.assertEqual(sorted(b.pages), [5, 6, 7, 8])

    def test_248258_pages_9_to_11(self):
        pages = self._make_detection_results()
        result = group_detections(pages, self.WHITELIST)
        b = next(b for b in result.blocks if b.ticket == "248258")
        self.assertEqual(sorted(b.pages), [9, 10, 11])

    def test_248259_pages_12_to_14(self):
        pages = self._make_detection_results()
        result = group_detections(pages, self.WHITELIST)
        b = next(b for b in result.blocks if b.ticket == "248259")
        self.assertEqual(sorted(b.pages), [12, 13, 14])

    def test_248260_pages_15_to_17(self):
        pages = self._make_detection_results()
        result = group_detections(pages, self.WHITELIST)
        b = next(b for b in result.blocks if b.ticket == "248260")
        self.assertEqual(sorted(b.pages), [15, 16, 17])

    def test_inherited_unmatched_soft_flags_present(self):
        """Blocks with misread handwritten pages must carry INHERITED_UNMATCHED soft flag."""
        pages = self._make_detection_results()
        result = group_detections(pages, self.WHITELIST)
        # Each block that has a misread page should carry INHERITED_UNMATCHED
        for ticket in ["248256", "248258", "248259", "248260"]:
            b = next(b for b in result.blocks if b.ticket == ticket)
            self.assertIn(FLAG_INHERITED_UNMATCHED, b.flags,
                f"Block {ticket} should carry INHERITED_UNMATCHED for misread handwritten page")

    def test_tib_no_printed_cover_flag_absent(self):
        """In TIB mode, all blocks start with a printed cover, so NO_PRINTED_COVER must not appear."""
        pages = self._make_detection_results()
        # Default batch_type="tib"
        result = group_detections(pages, self.WHITELIST)
        for b in result.blocks:
            self.assertNotIn("NO_PRINTED_COVER", b.flags,
                f"Block {b.ticket} should not have NO_PRINTED_COVER because page {b.pages[0]} has printed source")

    def test_tib_no_printed_cover_flag_present_when_missing(self):
        """In TIB mode, if a block starts with a handwritten page, it must get NO_PRINTED_COVER."""
        pages = self._make_detection_results()
        # Modify the first page of 248256 block (page 5) to be handwritten instead of printed
        pages[4]["candidates"][0]["source"] = "handwritten"
        result = group_detections(pages, self.WHITELIST)
        b = next(b for b in result.blocks if b.ticket == "248256")
        self.assertIn("NO_PRINTED_COVER", b.flags,
            "Block starting with handwritten page must carry NO_PRINTED_COVER in TIB mode")


# ─────────────────────────────────────────────────────────────────────────────
# Test 19: Non-TIB Pink Marker Boundary
# ─────────────────────────────────────────────────────────────────────────────

class TestNonTibPinkMarker(unittest.TestCase):
    def test_pink_marker_flag_in_non_tib_mode(self):
        """In non_tib mode, a page with pink_marker=True at the start of a block gets PINK_MARKER flag."""
        whitelist = ["111111", "222222"]
        pages = [
            make_page(1, value="111111", pink_marker=True),
            make_page(2, value="111111", pink_marker=False),
            make_page(3, value="222222", pink_marker=True),
        ]
        result = group_detections(pages, whitelist, batch_type="non_tib")
        
        b1 = next(b for b in result.blocks if b.ticket == "111111")
        b2 = next(b for b in result.blocks if b.ticket == "222222")
        
        self.assertIn("PINK_MARKER", b1.flags)
        self.assertIn("PINK_MARKER", b2.flags)

    def test_pink_marker_ignored_in_tib_mode(self):
        """In tib mode, pink_marker=True is ignored (no PINK_MARKER flag)."""
        whitelist = ["111111"]
        pages = [make_page(1, value="111111", pink_marker=True)]
        result = group_detections(pages, whitelist, batch_type="tib")
        self.assertNotIn("PINK_MARKER", result.blocks[0].flags)


# ─────────────────────────────────────────────────────────────────────────────
# Test 20: Phase B reconciliation logic — server-side validation rules
# These tests verify the reconciliation checks that gate the final download.
# They test the pure logic (no HTTP calls) by simulating the review state
# that the server-side confirm_job endpoint validates.
# ─────────────────────────────────────────────────────────────────────────────

def _make_review_state(whitelist, ticket_assignments):
    """
    Build a minimal review state dict for reconciliation tests.
    ticket_assignments: list of (block_id, ticket_or_None, has_hard_flag)
    """
    blocks = []
    for i, (bid, ticket, hard) in enumerate(ticket_assignments):
        blocks.append({
            "id": bid,
            "ticket": ticket,
            "pages": [i + 1],
            "flags": (["UNMATCHED_NUMBER"] if hard else []),
            "has_hard_flag": hard,
            "page_range": str(i + 1),
            "detection_sources": [],
            "max_confidence": 0.95,
            "unmatched_raw": [],
            "corrected_from": None,
            "suggestion": None,
            "neighbor_suggestion": None,
        })
    assigned = {b["ticket"] for b in blocks if b["ticket"]}
    missing = [t for t in whitelist if t not in assigned]
    return {"blocks": blocks, "whitelist": whitelist, "missing_tickets": missing}


def _run_server_checks(review):
    """
    Replicate the server-side reconciliation checks from confirm_job.
    Returns list of error strings (empty = all pass).
    """
    from collections import Counter
    blocks = review["blocks"]
    wl = review.get("whitelist", [])
    errors = []
    # Check 1: no unresolved hard flags / unassigned blocks
    unresolved = [b for b in blocks if b["has_hard_flag"] or b["ticket"] is None]
    if unresolved:
        errors.append(f"unresolved:{len(unresolved)}")
    # Check 2: no missing tickets
    assigned = {b["ticket"] for b in blocks if b["ticket"]}
    missing = [t for t in wl if t not in assigned]
    if missing:
        errors.append(f"missing:{','.join(missing)}")
    # Check 3: no duplicates
    counts = Counter(b["ticket"] for b in blocks if b["ticket"])
    dupes = [t for t, c in counts.items() if c > 1 and t in set(wl)]
    if dupes:
        errors.append(f"duplicates:{','.join(dupes)}")
    # Check 4: no extras
    extras = [b["ticket"] for b in blocks if b["ticket"] and b["ticket"] not in set(wl)]
    if extras:
        errors.append(f"extras:{','.join(set(extras))}")
    return errors


class TestReconciliationChecks(unittest.TestCase):
    """
    Tests for the three reconciliation checks that gate the final download.
    These mirror the server-side logic in confirm_job and the client reconciliation screen.
    """

    WL = ["247799", "248256", "248258", "248259", "248260"]

    def test_clean_batch_passes_all_checks(self):
        """A perfectly clean 5-block TIB batch must pass all checks."""
        review = _make_review_state(self.WL, [
            (0, "247799", False),
            (1, "248256", False),
            (2, "248258", False),
            (3, "248259", False),
            (4, "248260", False),
        ])
        errors = _run_server_checks(review)
        self.assertEqual(errors, [], f"Expected no errors, got: {errors}")

    def test_missing_ticket_blocks_download(self):
        """If a whitelist ticket has no assigned block, download must be blocked."""
        review = _make_review_state(self.WL, [
            (0, "247799", False),
            (1, "248256", False),
            (2, "248258", False),
            (3, "248259", False),
            # 248260 is missing — no block assigned to it
        ])
        errors = _run_server_checks(review)
        self.assertTrue(any("missing" in e for e in errors),
            f"Expected 'missing' error for 248260, got: {errors}")

    def test_duplicate_ticket_blocks_download(self):
        """If two blocks carry the same ticket number, download must be blocked."""
        review = _make_review_state(self.WL, [
            (0, "247799", False),
            (1, "248256", False),
            (2, "248258", False),
            (3, "248259", False),
            (4, "248259", False),  # duplicate — same ticket as block 3
        ])
        errors = _run_server_checks(review)
        self.assertTrue(any("duplicates" in e for e in errors),
            f"Expected 'duplicates' error for 248259, got: {errors}")

    def test_unassigned_block_blocks_download(self):
        """If any block has ticket=None, download must be blocked."""
        review = _make_review_state(self.WL, [
            (0, "247799", False),
            (1, None, False),  # unassigned
            (2, "248258", False),
            (3, "248259", False),
            (4, "248260", False),
        ])
        errors = _run_server_checks(review)
        self.assertTrue(any("unresolved" in e for e in errors),
            f"Expected 'unresolved' error for unassigned block, got: {errors}")

    def test_hard_flag_blocks_download(self):
        """If any block has a hard flag, download must be blocked."""
        review = _make_review_state(self.WL, [
            (0, "247799", True),  # has hard flag
            (1, "248256", False),
            (2, "248258", False),
            (3, "248259", False),
            (4, "248260", False),
        ])
        errors = _run_server_checks(review)
        self.assertTrue(any("unresolved" in e for e in errors),
            f"Expected 'unresolved' error for hard-flagged block, got: {errors}")

    def test_extra_ticket_blocks_download(self):
        """If a block carries a ticket not in the whitelist, download must be blocked."""
        review = _make_review_state(self.WL, [
            (0, "247799", False),
            (1, "248256", False),
            (2, "248258", False),
            (3, "248259", False),
            (4, "999999", False),  # not in whitelist
        ])
        errors = _run_server_checks(review)
        self.assertTrue(any("extras" in e for e in errors),
            f"Expected 'extras' error for 999999, got: {errors}")

    def test_17page_tib_fixture_passes_all_checks(self):
        """The 17-page TIB fixture (fixture #5) must pass all reconciliation checks."""
        wl = ["247799", "248256", "248258", "248259", "248260"]
        review = _make_review_state(wl, [
            (0, "247799", False),
            (1, "248256", False),
            (2, "248258", False),
            (3, "248259", False),
            (4, "248260", False),
        ])
        errors = _run_server_checks(review)
        self.assertEqual(errors, [],
            f"17-page TIB fixture must pass all reconciliation checks, got: {errors}")

    def test_right_arrow_on_unassigned_does_not_advance(self):
        """
        Spec: right-arrow on unassigned block does not advance — it moves focus to identity control.
        This is a UI rule; we verify the underlying condition: a block with ticket=None
        must be treated as 'cannot confirm' by the server checks.
        """
        wl = ["247799", "248256"]
        review = _make_review_state(wl, [
            (0, None, False),   # unassigned — right-arrow must not advance
            (1, "248256", False),
        ])
        errors = _run_server_checks(review)
        # The unassigned block must cause a check failure (blocking download)
        self.assertTrue(any("unresolved" in e or "missing" in e for e in errors),
            f"Unassigned block must block download, got: {errors}")

    def test_reassignment_updates_missing_tickets(self):
        """
        After reassigning a block, missing_tickets must be recomputed.
        Simulates: block 0 was unassigned, then assigned to 247799.
        """
        wl = ["247799", "248256"]
        # Before reassignment: 247799 is missing
        review = _make_review_state(wl, [
            (0, None, False),
            (1, "248256", False),
        ])
        self.assertIn("247799", review["missing_tickets"])
        # After reassignment: update block 0 ticket and recompute
        review["blocks"][0]["ticket"] = "247799"
        review["blocks"][0]["has_hard_flag"] = False
        assigned = {b["ticket"] for b in review["blocks"] if b["ticket"]}
        review["missing_tickets"] = [t for t in wl if t not in assigned]
        self.assertNotIn("247799", review["missing_tickets"])
        errors = _run_server_checks(review)
        self.assertEqual(errors, [],
            f"After reassignment, all checks should pass, got: {errors}")


# ─────────────────────────────────────────────────────────────────────────────
# Test 21 — DETECTION_FAILED hard flag (API error must never silently inherit)
# ─────────────────────────────────────────────────────────────────────────────

from grouping import FLAG_DETECTION_FAILED


def make_failed_page(page_num, error="max_retries_exceeded: Connection timeout"):
    """Build a raw detection dict simulating an API failure on one page."""
    return {"page": page_num, "candidates": [], "pink_marker": False, "error": error}


class TestDetectionFailed(unittest.TestCase):
    """
    Verify that a page whose API detection failed after all retries:
      1. Carries the DETECTION_FAILED hard flag
      2. Is never silently inherited from a neighbor
      3. Blocks Confirm (has_hard_flag = True)
      4. Is isolated in its own block (does not merge with adjacent pages)
    """

    WL = ["111111", "222222", "333333"]

    def _run(self, pages):
        result = group_detections(pages, self.WL)
        return result

    def test_failed_page_gets_detection_failed_flag(self):
        """A page with error set must carry DETECTION_FAILED flag."""
        pages = [
            make_page(1, "111111"),
            make_failed_page(2),
            make_page(3, "111111"),
        ]
        result = self._run(pages)
        # Find the block containing page 2
        failed_blocks = [b for b in result.blocks if 2 in b.pages]
        self.assertEqual(len(failed_blocks), 1, "Page 2 must be in exactly one block")
        failed_block = failed_blocks[0]
        self.assertIn(FLAG_DETECTION_FAILED, failed_block.flags,
                      "DETECTION_FAILED must be in the block's flags")

    def test_failed_page_is_not_inherited(self):
        """A failed page must NOT inherit the ticket from its neighbor."""
        pages = [
            make_page(1, "111111"),
            make_failed_page(2),
            make_page(3, "111111"),
        ]
        result = self._run(pages)
        failed_blocks = [b for b in result.blocks if 2 in b.pages]
        self.assertEqual(len(failed_blocks), 1)
        failed_block = failed_blocks[0]
        self.assertIsNone(failed_block.ticket,
                          "Failed page must NOT inherit ticket — ticket must be None")

    def test_failed_page_blocks_confirm(self):
        """A batch with a DETECTION_FAILED page must have has_hard_flag=True."""
        pages = [
            make_page(1, "111111"),
            make_failed_page(2),
            make_page(3, "222222"),
        ]
        result = self._run(pages)
        failed_blocks = [b for b in result.blocks if 2 in b.pages]
        self.assertEqual(len(failed_blocks), 1)
        self.assertTrue(failed_blocks[0].has_hard_flag,
                        "Block with DETECTION_FAILED must have has_hard_flag=True")

    def test_failed_page_is_isolated_block(self):
        """A failed page must be in its own block, not merged with adjacent pages."""
        pages = [
            make_page(1, "111111"),
            make_failed_page(2),
            make_page(3, "111111"),
        ]
        result = self._run(pages)
        # Page 2 must be alone in its block
        failed_blocks = [b for b in result.blocks if 2 in b.pages]
        self.assertEqual(len(failed_blocks), 1)
        self.assertEqual(failed_blocks[0].pages, [2],
                         "Failed page must be isolated in its own single-page block")

    def test_failed_page_in_middle_does_not_break_adjacent_blocks(self):
        """Pages before and after a failed page are still correctly grouped."""
        pages = [
            make_page(1, "111111"),
            make_page(2, "111111"),
            make_failed_page(3),
            make_page(4, "222222"),
            make_page(5, "222222"),
        ]
        result = self._run(pages)
        tickets = {b.ticket: b.pages for b in result.blocks if b.ticket}
        self.assertIn("111111", tickets)
        self.assertIn("222222", tickets)
        self.assertEqual(tickets["111111"], [1, 2])
        self.assertEqual(tickets["222222"], [4, 5])

    def test_multiple_failed_pages_each_isolated(self):
        """Multiple failed pages each get their own isolated block."""
        pages = [
            make_page(1, "111111"),
            make_failed_page(2, error="max_retries_exceeded: timeout"),
            make_failed_page(3, error="max_retries_exceeded: 500 server error"),
            make_page(4, "222222"),
        ]
        result = self._run(pages)
        failed_blocks = [b for b in result.blocks if FLAG_DETECTION_FAILED in b.flags]
        self.assertEqual(len(failed_blocks), 2,
                         "Each failed page must be in its own isolated block")
        failed_pages = sorted(p for b in failed_blocks for p in b.pages)
        self.assertEqual(failed_pages, [2, 3])

    def test_clean_batch_has_no_detection_failed(self):
        """A batch with no API errors must have no DETECTION_FAILED flags."""
        pages = [
            make_page(1, "111111"),
            make_page(2, "111111"),
            make_page(3, "222222"),
        ]
        result = self._run(pages)
        for block in result.blocks:
            self.assertNotIn(FLAG_DETECTION_FAILED, block.flags,
                             f"Clean batch block {block.ticket} must not have DETECTION_FAILED")


if __name__ == "__main__":
    unittest.main()


# ─────────────────────────────────────────────────────────────────────────────
# Test 22 — Fast Mode: FLAG_NOT_READ semantics
# ─────────────────────────────────────────────────────────────────────────────

from grouping import FLAG_NOT_READ


def make_not_read_page(page_num):
    """Build a raw detection dict simulating a fast-mode not_read page."""
    return {"page": page_num, "candidates": [], "pink_marker": False, "error": None, "not_read": True}


class TestFastModeNotRead(unittest.TestCase):
    """
    Verify fast mode not_read semantics:
      1. not_read pages inherit ticket from previous block (soft flag only)
      2. not_read pages carry FLAG_NOT_READ (soft, not in HARD_FLAGS)
      3. not_read pages do NOT block Confirm (has_hard_flag = False)
      4. A forgotten sticker (no boundary) causes MISSING_TICKET — blocks download
      5. DETECTION_FAILED on first page: block unidentified, inner pages still not_read
    """

    WL = ["111111", "222222", "333333"]

    def _run(self, pages):
        return group_detections(pages, self.WL)

    def test_not_read_page_inherits_ticket(self):
        """A not_read page must inherit the ticket from the previous resolved page."""
        pages = [
            make_page(1, "111111"),
            make_not_read_page(2),
            make_not_read_page(3),
        ]
        result = self._run(pages)
        # All pages should be in the 111111 block
        blocks_111 = [b for b in result.blocks if b.ticket == "111111"]
        self.assertEqual(len(blocks_111), 1, "111111 block must exist")
        self.assertIn(2, blocks_111[0].pages, "Page 2 (not_read) must inherit into 111111 block")
        self.assertIn(3, blocks_111[0].pages, "Page 3 (not_read) must inherit into 111111 block")

    def test_not_read_page_carries_flag(self):
        """A not_read page must carry FLAG_NOT_READ in its block's flags."""
        pages = [
            make_page(1, "111111"),
            make_not_read_page(2),
        ]
        result = self._run(pages)
        blocks_111 = [b for b in result.blocks if b.ticket == "111111"]
        self.assertEqual(len(blocks_111), 1)
        # The block should carry NOT_READ flag (from the not_read page)
        self.assertIn(FLAG_NOT_READ, blocks_111[0].flags,
                      "Block containing a not_read page must have FLAG_NOT_READ")

    def test_not_read_flag_is_soft_not_hard(self):
        """FLAG_NOT_READ must NOT be in HARD_FLAGS — it must not block Confirm."""
        from grouping import HARD_FLAGS
        self.assertNotIn(FLAG_NOT_READ, HARD_FLAGS,
                         "FLAG_NOT_READ must be a soft flag, not a hard flag")

    def test_not_read_does_not_block_confirm(self):
        """A batch with only not_read inner pages must not have has_hard_flag=True."""
        pages = [
            make_page(1, "111111"),
            make_not_read_page(2),
            make_not_read_page(3),
            make_page(4, "222222"),
            make_not_read_page(5),
        ]
        result = self._run(pages)
        for block in result.blocks:
            if block.ticket in ("111111", "222222"):
                self.assertFalse(
                    block.has_hard_flag,
                    f"Block {block.ticket} with not_read pages must not have has_hard_flag=True"
                )

    def test_forgotten_sticker_causes_missing_ticket(self):
        """
        If a pink sticker is missed (no boundary), pages that should be a new block
        are absorbed into the previous block. The missing ticket then fails the
        reconciliation check (MISSING_TICKET).
        """
        # Simulate: ticket 111111 has pages 1-3, but page 4 (start of 222222) has
        # no detected ticket (sticker missed). Pages 4-5 get absorbed into 111111.
        # 222222 is never assigned → MISSING_TICKET.
        pages = [
            make_page(1, "111111"),
            make_not_read_page(2),
            make_not_read_page(3),
            make_not_read_page(4),  # 222222 sticker missed — absorbed into 111111
            make_not_read_page(5),
        ]
        result = self._run(pages)
        # 222222 should be in missing_tickets since it was never detected
        self.assertIn("222222", result.missing_tickets,
                      "Missed sticker must result in 222222 appearing in missing_tickets")

    def test_detection_failed_on_first_page_inner_pages_not_read(self):
        """
        If the first page of a block has DETECTION_FAILED, the block is unidentified
        (hard flag). Inner pages can still be not_read (they don't inherit the error).
        """
        pages = [
            make_failed_page(1),   # first page of block — DETECTION_FAILED
            make_not_read_page(2), # inner page — not_read, no error
            make_not_read_page(3), # inner page — not_read, no error
            make_page(4, "222222"),
        ]
        result = self._run(pages)
        # Page 1 must be in an isolated DETECTION_FAILED block
        failed_blocks = [b for b in result.blocks if FLAG_DETECTION_FAILED in b.flags]
        self.assertGreater(len(failed_blocks), 0, "DETECTION_FAILED block must exist for page 1")
        # Pages 2 and 3 are not_read — they should NOT carry DETECTION_FAILED
        for b in result.blocks:
            if 2 in b.pages or 3 in b.pages:
                self.assertNotIn(FLAG_DETECTION_FAILED, b.flags,
                                 "not_read inner pages must not carry DETECTION_FAILED")

    def test_not_read_pages_before_first_detection_get_orphan_flag(self):
        """
        not_read pages before the first detection (no previous ticket) must get
        FLAG_ORPHAN_LEADING_PAGES in addition to FLAG_NOT_READ.
        """
        from grouping import FLAG_ORPHAN_LEADING_PAGES
        pages = [
            make_not_read_page(1),  # before any detection
            make_not_read_page(2),  # before any detection
            make_page(3, "111111"),
        ]
        result = self._run(pages)
        # Pages 1 and 2 should be orphan
        orphan_blocks = [b for b in result.blocks if FLAG_ORPHAN_LEADING_PAGES in b.flags]
        orphan_pages = [p for b in orphan_blocks for p in b.pages]
        self.assertIn(1, orphan_pages, "not_read page before first detection must be ORPHAN")
        self.assertIn(2, orphan_pages, "not_read page before first detection must be ORPHAN")


# ─────────────────────────────────────────────────────────────────────────────
# Test 20: Regression fixture #6 — SKM_C250i26071015110(1).pdf (16 pages)
#
# Ground truth: human-confirmed session 2026-07-16, job 67fe40bd-…
# PDF: 16 pages, non_tib batch, 6 tickets, all pink-marker boundaries.
# Whitelist: 301053, 299198, 298404, 300588, 300871, 300291
#
# Page map (confirmed by owner):
#   301053 = pages 1–3
#   299198 = page 4
#   298404 = page 5
#   300588 = pages 6–14
#   300871 = page 15
#   300291 = page 16
#
# All blocks start with a pink sticker page (pink_marker=True on first page).
# No hard flags expected in the confirmed ground truth.
#
# NOTE: '301053' was previously mangled as '01053' in a discarded attempt.
# This test locks in the correct value.
# ─────────────────────────────────────────────────────────────────────────────

class TestRegressionFixture6(unittest.TestCase):
    """
    Regression fixture for SKM_C250i26071015110(1).pdf (16 pages).
    Ground truth confirmed by owner on 2026-07-16.
    Source: tests/fixtures/fixture6/ground_truth.json
    """

    WHITELIST = ["301053", "299198", "298404", "300588", "300871", "300291"]

    # Expected page→ticket mapping from confirmed_snapshot
    EXPECTED_PAGE_MAP = {
        1: "301053", 2: "301053", 3: "301053",
        4: "299198",
        5: "298404",
        6: "300588", 7: "300588", 8: "300588", 9: "300588",
        10: "300588", 11: "300588", 12: "300588", 13: "300588", 14: "300588",
        15: "300871",
        16: "300291",
    }

    # Expected blocks: (ticket, pages, has_pink_marker)
    EXPECTED_BLOCKS = [
        ("301053", [1, 2, 3],                          True),
        ("299198", [4],                                 True),
        ("298404", [5],                                 True),
        ("300588", [6, 7, 8, 9, 10, 11, 12, 13, 14],  True),
        ("300871", [15],                                True),
        ("300291", [16],                                True),
    ]

    def _make_detection_results(self):
        """
        Simulated detection results matching the confirmed ground truth.
        All block-start pages carry pink_marker=True; inner pages do not.
        All pages read the correct ticket number cleanly.
        """
        pages = []
        for ticket, page_list, _ in self.EXPECTED_BLOCKS:
            for i, pg in enumerate(page_list):
                is_first = (i == 0)
                pages.append(make_page(
                    pg, value=ticket,
                    source="printed", confidence=0.97,
                    pink_marker=is_first,
                ))
        # Sort by page number (they are already in order, but be explicit)
        pages.sort(key=lambda p: p["page"])
        return pages

    def test_block_count(self):
        """Exactly 6 blocks must be produced."""
        pages = self._make_detection_results()
        result = group_detections(pages, self.WHITELIST, batch_type="non_tib")
        self.assertEqual(len(result.blocks), 6,
            f"Expected 6 blocks, got {len(result.blocks)}: "
            f"{[(b.ticket, b.pages) for b in result.blocks]}")

    def test_page_map(self):
        """Every page must map to the correct ticket."""
        pages = self._make_detection_results()
        result = group_detections(pages, self.WHITELIST, batch_type="non_tib")
        actual_map = {}
        for b in result.blocks:
            for pg in b.pages:
                actual_map[pg] = b.ticket
        for pg, expected_ticket in self.EXPECTED_PAGE_MAP.items():
            self.assertEqual(
                actual_map.get(pg), expected_ticket,
                f"Page {pg}: expected ticket {expected_ticket}, got {actual_map.get(pg)}",
            )

    def test_all_blocks_have_pink_marker_flag(self):
        """Every block must carry PINK_MARKER flag (all start with a pink sticker page)."""
        pages = self._make_detection_results()
        result = group_detections(pages, self.WHITELIST, batch_type="non_tib")
        for b in result.blocks:
            self.assertIn("PINK_MARKER", b.flags,
                f"Block {b.ticket} (pages {b.pages}) missing PINK_MARKER flag")

    def test_no_hard_flags(self):
        """No block should carry any hard flag in the confirmed ground truth."""
        pages = self._make_detection_results()
        result = group_detections(pages, self.WHITELIST, batch_type="non_tib")
        for b in result.blocks:
            hard = [f for f in b.flags if f in HARD_FLAGS]
            self.assertEqual(hard, [],
                f"Block {b.ticket} has unexpected hard flags: {hard}")

    def test_no_missing_tickets(self):
        """All 6 whitelist tickets must appear in the result."""
        pages = self._make_detection_results()
        result = group_detections(pages, self.WHITELIST, batch_type="non_tib")
        assigned = {b.ticket for b in result.blocks}
        for ticket in self.WHITELIST:
            self.assertIn(ticket, assigned,
                f"Whitelist ticket {ticket} not found in any block")

    def test_whitelist_301053_not_mangled(self):
        """Explicit guard: '301053' must appear in the whitelist, not '01053'."""
        self.assertIn("301053", self.WHITELIST)
        self.assertNotIn("01053", self.WHITELIST)

    def test_non_tib_mode_required(self):
        """Fixture #6 is non_tib — running it in tib mode must not produce the same clean result."""
        pages = self._make_detection_results()
        result_tib = group_detections(pages, self.WHITELIST, batch_type="tib")
        result_non_tib = group_detections(pages, self.WHITELIST, batch_type="non_tib")
        # In non_tib mode all blocks have PINK_MARKER; in tib mode they should not
        non_tib_pink = all("PINK_MARKER" in b.flags for b in result_non_tib.blocks)
        tib_pink = all("PINK_MARKER" in b.flags for b in result_tib.blocks)
        self.assertTrue(non_tib_pink, "non_tib mode must produce PINK_MARKER on all blocks")
        self.assertFalse(tib_pink, "tib mode must NOT produce PINK_MARKER flags")
