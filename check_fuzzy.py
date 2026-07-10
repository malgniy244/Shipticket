"""
Quick check: does the spec's edit-distance-1 whitelist fuzzy match
correctly resolve the misread values from pages 7 and 10?

Whitelist: 301532, 257535, 253983, 258066, 257086
Misreads:  37983 (p7 run1), 247983 (p7 run2), 237983 (p7 run3), 243983 (p10)
"""


def digit_edit_distance(a: str, b: str) -> int:
    """
    Levenshtein distance between two digit strings.
    """
    m, n = len(a), len(b)
    dp = list(range(n + 1))
    for i in range(1, m + 1):
        prev = dp[:]
        dp[0] = i
        for j in range(1, n + 1):
            if a[i - 1] == b[j - 1]:
                dp[j] = prev[j - 1]
            else:
                dp[j] = 1 + min(prev[j - 1], prev[j], dp[j - 1])
    return dp[n]


def fuzzy_match(detected: str, whitelist: list[str]) -> list[str]:
    """
    Returns whitelist entries within edit distance 1 of detected.
    Exact match first; then edit-distance-1 matches.
    """
    if detected in whitelist:
        return [detected]
    return [w for w in whitelist if digit_edit_distance(detected, w) <= 1]


WHITELIST = ["301532", "257535", "253983", "258066", "257086"]

test_cases = [
    ("37983",  "page 7 run1 — 5-digit truncation"),
    ("247983", "page 7 run2"),
    ("237983", "page 7 run3"),
    ("243983", "page 10"),
    ("253983", "correct value — should exact-match"),
]

print(f"{'Detected':<12} {'Matches':<30} {'Resolves?':<10} Note")
print("-" * 75)
for detected, note in test_cases:
    matches = fuzzy_match(detected, WHITELIST)
    resolves = "YES (unique)" if len(matches) == 1 else ("AMBIGUOUS" if len(matches) > 1 else "NO MATCH")
    print(f"{detected:<12} {str(matches):<30} {resolves:<10} {note}")
