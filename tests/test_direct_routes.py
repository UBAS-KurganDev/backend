"""Direct flights compete by price, without quotas or deduplication."""

import unittest

from planner_fixtures import NoVisas, offer, plan, profile, segment


class DirectRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_works_without_tourism_or_visa_and_has_no_fictitious_stop(
        self,
    ):
        result = await plan([offer("d", [segment("AAA", "ZZZ")], 100)])
        route = result.routes[0].route
        self.assertEqual(route.kind, "direct")
        self.assertEqual(route.stopovers, ())
        self.assertIsNone(route.stopover)
        self.assertIsNone(route.visa)
        self.assertEqual(route.visa_cost.amount, 0)
        self.assertEqual(route.total_trip_hours, 2)
        self.assertTrue(route.full_cost_known)

    async def test_ten_cheapest_duplicates_are_retained_with_distinct_card_ids(self):
        direct = offer("same", [segment("AAA", "ZZZ")], 100)
        through = offer("t", [segment("AAA", "BBB"), segment("BBB", "ZZZ", 4)], 101)
        result = await plan([direct] * 11 + [through], visas=NoVisas())
        self.assertEqual(len(result.routes), 10)
        self.assertEqual({r.route.kind for r in result.routes}, {"direct"})
        self.assertEqual(len({r.route.id for r in result.routes}), 10)

    async def test_cheaper_stopover_precedes_direct_regardless_of_tourism(self):
        result = await plan(
            [
                offer("d", [segment("AAA", "ZZZ")], 100),
                offer("t", [segment("AAA", "BBB"), segment("BBB", "ZZZ", 4)], 99),
            ],
            profiles=[profile("BBB", 0)],
            visas=NoVisas(),
        )
        self.assertEqual([r.route.kind for r in result.routes], ["stopover", "direct"])
        self.assertTrue(all(r.score is None for r in result.routes))
