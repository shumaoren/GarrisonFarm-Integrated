from dataclasses import replace
import unittest

from 整合版.程序.level1_promotion import (
    OTHER_TILES, SAFE_TILES, Offer, Snapshot, Unit, decide, settled_promotions,
    snapshot_from_dict,
)


def shop(*names):
    return tuple(Offer(index, name, 1, 2) for index, name in enumerate(names))


def reward(*names):
    return tuple(Offer(index, name, 2, 0) for index, name in enumerate(names))


def unit(uid, name, tier=1, role="近卫", elite=False, tile=None):
    return Unit(uid, name, tier, role, elite, tile)


def state(**overrides):
    return replace(Snapshot(round_number=4, funds=2, hp=20, bench_free=4), **overrides)


def fixed_board(*, elite=True):
    return tuple(
        unit(f"board-{index}", f"场上{index}", tier=2, elite=elite, tile=tile)
        for index, tile in enumerate(SAFE_TILES + OTHER_TILES)
    )


def board_with_tanks():
    board = fixed_board()
    return (
        unit("tank-0", "拜松", role="重装", tile=SAFE_TILES[0]),
        unit("tank-1", "拜松", role="重装", tile=SAFE_TILES[1]),
    ) + board[2:]


class OpeningTests(unittest.TestCase):
    def test_confirm_squad_before_first_shop_data_is_available(self):
        scene = snapshot_from_dict({"phase": "squad_setup"})
        action = decide(scene)
        self.assertEqual(action.kind, "confirm_squad")
        self.assertIsNone(action.slot)
        self.assertIsNone(action.unit_id)

    def test_unknown_screen_does_not_confirm_squad(self):
        self.assertEqual(decide(state(phase="unknown")).kind, "wait")


class ShoppingTests(unittest.TestCase):
    def test_round_one_buys_all_three_before_deploying(self):
        scene = state(round_number=1, funds=6, shop=shop("折桠", "褐果", "灰喉"))
        picked = []
        for _ in range(3):
            action = decide(scene)
            self.assertEqual(action.kind, "buy")
            card = next(card for card in scene.shop if card.slot == action.slot)
            picked.append(card.name)
            scene = replace(
                scene, funds=scene.funds - 2, bench_free=scene.bench_free - 1,
                shop=tuple(other for other in scene.shop if other.slot != card.slot),
                units=scene.units + (unit(card.name, card.name),),
            )
        self.assertEqual(picked, ["折桠", "褐果", "灰喉"])

    def test_will_not_buy_gear_or_upgrade(self):
        scene = state(funds=2, shop=(Offer(3, "盟约之币", 1, 1),))
        self.assertEqual(decide(scene).kind, "refresh")
        self.assertEqual(decide(replace(scene, shop_level=2)).kind, "wait")

    def test_round_two_and_three_buy_unrelated_stock_even_with_an_existing_pair(self):
        for number, funds in ((2, 7), (3, 8)):
            with self.subTest(round=number):
                scene = state(
                    round_number=number, funds=funds,
                    units=(unit("a", "灰喉", tile="path0"), unit("b", "灰喉", tile="path1")),
                    shop=shop("夏栎", "褐果", "慕斯"),
                )
                for index, card in enumerate(scene.shop):
                    action = decide(scene)
                    self.assertEqual((action.kind, action.slot), ("buy", index))
                    scene = replace(
                        scene, funds=scene.funds - card.cost, bench_free=scene.bench_free - 1,
                        units=scene.units + (unit(str(index), card.name),), shop=scene.shop[1:],
                    )
                self.assertEqual(decide(scene).kind, "refresh")
                scene = replace(scene, funds=scene.funds - 1, paid_refreshes=1, freeze_refresh_used=True)
                if scene.funds == 1:
                    self.assertEqual(decide(scene).kind, "refresh")
                    scene = replace(scene, funds=0, paid_refreshes=2)
                self.assertEqual(decide(scene).kind, "deploy")

    def test_round_two_buys_promotion_before_unrelated_cards(self):
        scene = state(
            round_number=2, funds=7,
            units=(unit("a", "灰喉", tile="path0"), unit("b", "灰喉", tile="path1")),
            shop=shop("夏栎", "灰喉", "慕斯"),
        )
        self.assertEqual((decide(scene).kind, decide(scene).slot), ("buy", 1))

    def test_uses_leftover_funds_when_shop_has_no_recruits(self):
        for round_number in range(1, 9):
            with self.subTest(round=round_number):
                scene = state(round_number=round_number, funds=4, units=fixed_board(elite=False))
                for funds in (4, 3, 2, 1):
                    self.assertEqual(decide(replace(scene, funds=funds)).kind, "refresh")
                self.assertEqual(decide(replace(scene, funds=0)).kind, "ready")

    def test_round_eight_refreshes_last_coin_even_when_unaffordable_recruits_remain(self):
        scene = state(round_number=8, funds=1, units=fixed_board(elite=False), shop=shop("灰喉", "慕斯"))
        self.assertEqual(decide(scene).kind, "refresh")
        self.assertEqual(decide(replace(scene, funds=0)).kind, "ready")

    def test_final_promotion_sells_only_until_the_current_shop_deal_is_funded(self):
        pair = (unit("first", "灰喉", tile="path0"), unit("second", "灰喉"))
        donors = (
            unit("tank", "泡泡", tier=2, role="重装", tile="path1"),
            unit("medic", "哈洛德", tier=2, role="医疗"),
            unit("spare", "卡达"),
        )
        scene = state(round_number=8, funds=0, bench_free=7, units=pair + donors, shop=shop("灰喉"))
        for donor in donors[:2]:
            action = decide(scene)
            self.assertEqual((action.kind, action.unit_id, action.promotion_target),
                             ("sell", donor.id, "灰喉"))
            scene = replace(scene, funds=scene.funds + 1, final_promotion_target=action.promotion_target,
                            bench_free=scene.bench_free + (donor.tile is None),
                            units=tuple(unit for unit in scene.units if unit.id != donor.id))
        self.assertEqual((decide(scene).kind, decide(scene).slot), ("buy", 0))
        finished = replace(scene, final_promotion_target=None, final_promotion_done=True,
                           units=donors[-1:] + (unit("another", "卡达"),), shop=shop("卡达"))
        self.assertEqual(decide(finished).kind, "ready")

    def test_final_promotion_requires_more_spares_than_the_funding_gap(self):
        pair = (unit("first", "灰喉", tile="path0"), unit("second", "灰喉"))
        spares = (unit("one", "卡达"), unit("two", "慕斯"))
        scene = state(round_number=8, funds=0, bench_free=7, units=pair + spares, shop=shop("灰喉"))
        self.assertEqual(decide(scene).kind, "ready")
        with_one_fund = replace(scene, funds=1)
        self.assertEqual(decide(with_one_fund).promotion_target, "灰喉")
        previous_round = replace(with_one_fund, round_number=7, blocked_tiles=SAFE_TILES + OTHER_TILES)
        self.assertEqual(decide(previous_round).kind, "freeze")
        self.assertEqual(decide(replace(scene, shop=shop("卡达"))).kind, "ready")

    def test_final_promotion_keeps_its_target_when_other_deals_are_available(self):
        units = (unit("one", "灰喉"), unit("two", "卡达"), unit("three", "卡达"))
        scene = state(round_number=8, funds=4, units=units, shop=shop("卡达", "灰喉", "灰喉"),
                      final_promotion_target="灰喉")
        self.assertEqual((decide(scene).kind, decide(scene).slot), ("buy", 1))
        self.assertEqual(decide(replace(scene, shop=shop("卡达"))).kind, "wait")

    def test_two_funds_keep_priority_purchase_and_last_coin_can_still_refresh(self):
        scene = state(
            round_number=5, funds=2, paid_refreshes=3,
            units=(unit("a", "灰喉", tile="path0"), unit("b", "灰喉", tile="path1")),
            shop=shop("慕斯", "陨星", "角峰"),
        )
        self.assertEqual(decide(scene).kind, "refresh")
        self.assertEqual(decide(replace(scene, hp=10)).kind, "buy")
        target = replace(scene, shop=shop("慕斯", "灰喉", "陨星"))
        self.assertEqual((decide(target).kind, decide(target).slot), ("buy", 1))
        spent = replace(scene, funds=1, paid_refreshes=4, freeze_refresh_used=True)
        self.assertEqual(decide(spent).kind, "refresh")
        self.assertEqual(decide(replace(spent, shop=target.shop)).kind, "freeze")

    def test_prefer_cheapest_immediate_merge_over_building_another_pair(self):
        scene = state(
            funds=6,
            units=(unit("a", "灰喉", tile="path1"), unit("a2", "灰喉"), unit("b", "折桠")),
            shop=shop("折桠", "折桠", "灰喉"),
        )
        self.assertEqual((decide(scene).kind, decide(scene).slot), ("buy", 2))

    def test_prepare_two_spaces_for_double_purchase(self):
        scene = state(funds=4, bench_free=1, units=(unit("a", "灰喉"),), shop=shop("灰喉", "灰喉"))
        self.assertEqual(decide(scene).kind, "deploy")
        after_move = replace(
            scene, bench_free=2, units=(replace(scene.units[0], tile="path0"),),
        )
        self.assertEqual(decide(after_move).kind, "buy")

    def test_early_new_pair_vs_late_existing_pair(self):
        scene = state(
            funds=8, hp=30, expected_future_shops=2,
            units=(unit("a", "折桠"), unit("b", "折桠")),
            shop=shop("灰喉", "灰喉"),
        )
        self.assertEqual(decide(scene).kind, "buy")
        self.assertEqual(decide(replace(scene, round_number=7)).kind, "refresh")

    def test_round_six_and_seven_seed_up_to_two_ordinary_names_when_no_pair_exists(self):
        for number in (6, 7):
            with self.subTest(round=number):
                scene = state(
                    round_number=number, funds=6, hp=30,
                    units=(unit("a", "灰喉", elite=True, tile="path0"),),
                    shop=shop("灰喉", "慕斯", "褐果"),
                )
                self.assertEqual((decide(scene).kind, decide(scene).slot), ("buy", 0))
                scene = replace(scene, funds=4, shop=scene.shop[1:],
                                units=scene.units + (unit("b", "灰喉", tile="path1"),))
                self.assertEqual((decide(scene).kind, decide(scene).slot), ("buy", 1))
                scene = replace(scene, shop=scene.shop[1:],
                                units=scene.units + (unit("c", "慕斯", tile="path2"),))
                self.assertEqual(decide(scene).kind, "refresh")

    def test_last_shop_preserves_enough_to_pay_for_a_possible_merge(self):
        scene = state(round_number=8, funds=6, hp=40, shop=shop("灰喉", "慕斯", "褐果"))
        self.assertEqual(decide(scene).reason, "开战前剩6资金，刷新找牌")
        self.assertEqual(decide(replace(scene, funds=7)).reason, "保留刷新后购买3张Ⅰ阶牌的资金")
        scene = replace(scene, funds=4, units=(unit("a", "灰喉", tile="path0"),),
                        shop=shop("角峰", "慕斯", "褐果"))
        self.assertEqual(decide(scene).reason, "开战前剩4资金，刷新找牌")
        self.assertEqual(decide(replace(scene, funds=5)).reason, "保留刷新后购买2张Ⅰ阶牌的资金")
        scene = replace(scene, shop=shop("灰喉", "灰喉", "褐果"))
        self.assertEqual(decide(scene).reason, "当前商店能完成晋升")

    def test_round_eight_starts_one_name_only_with_nine_funds_and_no_tier_one_stock(self):
        existing = unit("reward", "哈洛德", tier=2, role="医疗")
        scene = state(round_number=8, funds=9, units=(existing,), shop=shop("灰喉", "慕斯", "褐果"))
        action = decide(scene)
        self.assertEqual((action.kind, action.slot), ("buy", 0))
        self.assertEqual(decide(replace(scene, funds=8)).kind, "refresh")
        self.assertEqual(decide(replace(scene, shop=shop("慕斯", "灰喉", "灰喉"))).slot, 1)
        self.assertEqual(decide(replace(scene, shop=shop("慕斯", "灰喉", "灰喉"))).reason, "买成对子")

        with_single = replace(scene, funds=11, units=(existing, unit("first", "灰喉")), shop=shop("慕斯", "褐果"))
        self.assertEqual(decide(with_single).kind, "refresh")
        match = decide(replace(with_single, shop=shop("慕斯", "灰喉")))
        self.assertEqual((match.kind, match.slot, match.reason), ("buy", 1, "买成对子"))

    def test_round_eight_does_not_spend_leftovers_on_a_pair_without_a_path_to_promotion(self):
        scene = state(round_number=8, funds=4, units=fixed_board(elite=False), shop=shop("陨星", "慕斯", "慕斯"))
        for funds in (4, 3, 2, 1):
            self.assertEqual(decide(replace(scene, funds=funds)).kind, "refresh")
        self.assertEqual(decide(replace(scene, funds=0)).kind, "ready")
        self.assertEqual(decide(replace(scene, funds=7)).reason, "买成对子")

    def test_ordinary_and_elite_forms_do_not_share_promotion_count(self):
        scene = state(
            units=(unit("a", "灰喉", elite=True), unit("b", "灰喉")), shop=shop("灰喉"),
        )
        self.assertEqual(decide(scene).reason, "买成对子")

    def test_full_inventory_frees_a_slot_before_refreshing(self):
        scene = state(
            funds=3, bench_free=0,
            units=board_with_tanks() + (unit("sale", "角峰", elite=True, role="重装"),),
            shop=shop("折桠"),
        )
        action = decide(scene)
        self.assertEqual((action.kind, action.unit_id), ("sell", "sale"))
        after_sale = replace(scene, bench_free=1, units=scene.units[:-1], funds=4)
        self.assertEqual(decide(after_sale).kind, "refresh")


class PlacementAndSaleTests(unittest.TestCase):
    def test_deployment_blacklist_applies_in_normal_and_overflow_placement(self):
        ordinary = unit("ordinary", "渡桥", 2)
        elite = replace(ordinary, id="elite", elite=True)
        other = unit("other", "摆渡人")
        scene = state(funds=0, units=(ordinary, elite, other))
        self.assertEqual((decide(scene).kind, decide(scene).unit_id), ("deploy", other.id))
        scene = replace(scene, units=(ordinary, elite))
        self.assertEqual(decide(scene).kind, "ready")

        reward_scene = replace(scene, phase="reward", bench_free=0, rewards=reward("渡桥"))
        self.assertEqual(decide(reward_scene).kind, "wait")
        self.assertEqual(decide(replace(reward_scene, bench_free=1)).kind, "claim_reward")

    def test_deployment_blacklist_also_blocks_moving_a_unit(self):
        placed = unit("placed", "渡桥", 2, tile="path0")
        scene = state(funds=0, bench_free=0, units=(placed,), stalled_unit_ids=(placed.id,))
        self.assertEqual(decide(scene).kind, "ready")

    def test_full_non_tank_board_immediately_sells_bench_elites_to_fund_a_merge(self):
        board = fixed_board()[:6] + (
            unit("pair-0", "灰喉", tile=OTHER_TILES[-2]),
            unit("pair-1", "灰喉", tile=OTHER_TILES[-1]),
        )
        ordinary = unit("ordinary", "褐果", role="医疗")
        elites = (unit("elite-1", "角峰", role="重装", elite=True), unit("elite-2", "真理", 2, elite=True))
        scene = state(funds=0, units=board + (ordinary,) + elites, shop=shop("灰喉"))
        for index, elite in enumerate(elites):
            action = decide(scene)
            self.assertEqual((action.kind, action.unit_id), ("sell", elite.id))
            scene = replace(scene, funds=index + 1, units=board + (ordinary,) + elites[index + 1:])
        self.assertEqual((decide(scene).kind, decide(scene).slot), ("buy", 0))

    def test_proactive_sale_requires_eight_placed_units_and_no_tanks(self):
        elite = unit("elite", "真理", 2, elite=True)
        board = fixed_board()
        scene = state(round_number=7, funds=0, units=board[:-1] + (elite,), blocked_tiles=(board[-1].tile,))
        self.assertEqual(decide(scene).kind, "ready")
        one_tank = (replace(board[0], profession="TANK"),) + board[1:]
        self.assertEqual(decide(replace(scene, units=one_tank + (elite,))).kind, "ready")

    def test_round_eight_sells_all_elites_before_spending_proceeds_on_a_promotion(self):
        tank = unit("field-elite", "蛇屠箱", 2, "重装", elite=True, tile="safe0")
        bench_elite = unit("bench-elite", "真理", 2, elite=True)
        pair = (unit("a", "灰喉", tile="path0"), unit("b", "灰喉"))
        scene = state(round_number=8, funds=0, units=(tank, bench_elite) + pair, shop=shop("灰喉"))
        for elite in (bench_elite, tank):
            action = decide(scene)
            self.assertEqual((action.kind, action.unit_id), ("sell", elite.id))
            scene = replace(scene, funds=scene.funds + 1,
                            units=tuple(unit for unit in scene.units if unit.id != elite.id))
        self.assertEqual((decide(scene).kind, decide(scene).slot), ("buy", 0))

    def test_round_eight_only_deploys_to_restore_a_free_bench_slot(self):
        first = unit("first", "灰喉")
        second = unit("second", "卡达")
        scene = state(round_number=8, funds=1, bench_free=1, units=(first, second))
        self.assertEqual(decide(scene).kind, "refresh")
        self.assertEqual(decide(replace(scene, funds=0)).kind, "ready")

        action = decide(replace(scene, funds=0, bench_free=0))
        self.assertEqual((action.kind, action.unit_id, action.tile), ("deploy", first.id, "path0"))
        after_deploy = replace(scene, units=(replace(first, tile=action.tile), second), funds=0)
        self.assertEqual(decide(after_deploy).kind, "ready")

        purchase = replace(scene, funds=4, units=(first, second), shop=shop("灰喉", "灰喉"))
        self.assertEqual((decide(purchase).kind, decide(purchase).slot), ("buy", 0))

    def test_round_eight_uses_sale_priority_when_both_areas_are_full(self):
        elite = unit("elite", "卡达", elite=True)
        tank = unit("tank", "角峰", role="重装", elite=True)
        board = fixed_board(elite=False)
        scene = state(round_number=8, funds=0, bench_free=0, units=(elite, tank) + board)
        self.assertEqual((decide(scene).kind, decide(scene).unit_id), ("sell", tank.id))
        with_field_space = replace(scene, units=(elite, tank) + board[:-1])
        self.assertEqual((decide(with_field_space).kind, decide(with_field_space).unit_id), ("sell", elite.id))

    def test_profession_exceptions_and_safe_slots(self):
        scene = state(
            funds=0,
            units=(unit("z", "折桠", role="重装"), unit("y", "夏栎", role="辅助"), unit("t", "角峰", role="重装")),
        )
        first = decide(scene)
        self.assertEqual((first.kind, first.unit_id, first.tile), ("deploy", "y", "safe0"))
        scene = replace(scene, units=(scene.units[0], replace(scene.units[1], tile="safe0"), scene.units[2]))
        self.assertEqual((decide(scene).unit_id, decide(scene).tile), ("z", "path0"))
        scene = replace(scene, units=(replace(scene.units[0], tile="path0"), scene.units[1], scene.units[2]))
        self.assertEqual(decide(scene).kind, "ready")

    def test_tier_two_uses_same_relaxation_order(self):
        occupants = (
            unit("s0", "灰喉", tile="safe0"),
            unit("s1", "远山", tier=2, tile="safe1"),
            unit("med", "哈洛德", tier=2, role="MEDIC"),
            unit("tank", "蛇屠箱", tier=2, role="TANK"),
            unit("target", "灰喉", tile="path2"),
        )
        scene = state(bench_free=0, units=occupants, shop=shop("灰喉"))
        first = decide(scene)
        self.assertEqual((first.kind, first.unit_id, first.tile), ("deploy", "med", "path0"))
        scene = replace(scene, units=occupants[:2] + (replace(occupants[2], tile="path0"),) + occupants[3:])
        second = decide(scene)
        self.assertEqual((second.kind, second.unit_id, second.tile), ("deploy", "tank", "path1"))

    def test_ju_follows_medic_placement_rules(self):
        for elite in (False, True):
            ju = unit("ju", "矩", 2, "狙击", elite)
            with self.subTest(elite=elite):
                self.assertEqual(ju.role, "medic")
                scene = state(funds=0, units=(ju,))
                self.assertEqual((decide(scene).unit_id, decide(scene).tile), ("ju", "safe0"))
                second_safe = replace(scene, blocked_tiles=("safe0",))
                self.assertEqual((decide(second_safe).unit_id, decide(second_safe).tile), ("ju", "safe1"))
                self.assertEqual(decide(replace(scene, blocked_tiles=SAFE_TILES)).kind, "ready")

        pair = (unit("one", "灰喉", tile="path2"), unit("two", "灰喉", tile="rest0"))
        overflow = state(funds=2, bench_free=0, units=(unit("ju", "矩", 2, "狙击"),) + pair,
                         blocked_tiles=SAFE_TILES, shop=shop("灰喉"))
        action = decide(overflow)
        self.assertEqual((action.kind, action.unit_id, action.tile), ("deploy", "ju", "path0"))

    def test_tank_on_non_safe_tile_can_clear_space_to_buy_a_pair(self):
        scene = state(
            bench_free=0, units=(
                unit("t", "角峰", role="重装"), unit("a", "灰喉", tile="path0"),
            ),
            shop=shop("灰喉"), blocked_tiles=SAFE_TILES,
        )
        action = decide(scene)
        self.assertEqual((action.kind, action.unit_id, action.tile), ("deploy", "t", "path1"))

    def test_never_break_placement_restrictions_before_bench_is_full(self):
        scene = state(
            funds=4, bench_free=1, units=(unit("t", "角峰", role="重装"),),
            shop=shop("角峰", "角峰"), blocked_tiles=SAFE_TILES,
        )
        self.assertEqual(decide(scene).kind, "buy")

    def test_stalled_unit_can_be_withdrawn_without_immediate_redeployment(self):
        unit_on_road = unit("t", "折桠", role="重装", tile="path0")
        scene = state(funds=0, units=(unit_on_road,), stalled_unit_ids=("t",))
        self.assertEqual(decide(scene).kind, "withdraw")
        after = replace(scene, units=(replace(unit_on_road, tile=None),))
        self.assertEqual(decide(after).kind, "ready")

    def test_both_areas_full_sell_by_copy_count_tier_and_role(self):
        candidates = (
            unit("first", "蛇屠箱", 2, "重装", True),
            unit("second", "夏栎", 1, "辅助", True),
            unit("third", "折桠", 1, "重装", True),
            unit("fourth", "绮良", 2, elite=True),
            unit("fifth", "哈洛德", 2, "医疗"),
            unit("sixth", "奥斯塔", 2),
            unit("seventh", "陨星"),
            unit("eighth", "蓝毒", 2),
            unit("ninth", "蓝毒", 2),
            unit("tenth", "角峰", 1, "重装"),
            unit("eleventh", "角峰", 1, "重装"),
        )
        protected = (unit("a", "灰喉"), unit("b", "灰喉"))
        for i, expected in enumerate(candidates):
            with self.subTest(rank=i):
                scene = state(bench_free=0, units=board_with_tanks() + protected + candidates[i:],
                              blocked_tiles=SAFE_TILES + OTHER_TILES, shop=shop("灰喉"))
                action = decide(scene)
                self.assertEqual((action.kind, action.unit_id), ("sell", expected.id))
        scene = replace(scene, units=board_with_tanks() + protected)
        self.assertEqual(decide(scene).kind, "freeze")

    def test_double_full_sale_order_applies_in_every_round_and_reward_phase(self):
        board = fixed_board(elite=False)
        bench = (unit("sale", "泡泡", tier=2, role="重装"),
                 unit("a", "灰喉"), unit("b", "灰喉"),
                 unit("c", "蓝毒", tier=2), unit("d", "蓝毒", tier=2))
        bench += tuple(unit(f"extra-{i}", f"候补{i}") for i in range(5))
        for number in range(1, 9):
            for phase in ("prep", "reward"):
                with self.subTest(round=number, phase=phase):
                    scene = state(round_number=number, funds=2, bench_free=0, phase=phase,
                                  units=board + bench, shop=shop("灰喉"), rewards=reward("蓝毒"))
                    action = decide(scene)
                    self.assertEqual((action.kind, action.unit_id, action.promotion_target),
                                     ("sell", "sale", None))

    def test_board_sale_only_when_the_vacancy_can_free_a_bench_slot(self):
        old_board = fixed_board()
        board = (unit("elite", "哈洛德", 2, "医疗", True, "safe0"),) + old_board[1:]
        pair = (unit("a", "灰喉"), unit("b", "灰喉"))
        scene = state(bench_free=0, units=board + pair, shop=shop("灰喉"))
        self.assertEqual(decide(scene).unit_id, "elite")
        after = replace(scene, units=old_board[1:] + pair)
        self.assertEqual((decide(after).kind, decide(after).tile), ("deploy", "safe0"))
        blocked = replace(scene, blocked_tiles=SAFE_TILES + OTHER_TILES)
        self.assertEqual(decide(blocked).kind, "freeze")


class RewardsFreezeAndExitTests(unittest.TestCase):
    def test_sell_new_bench_elite_before_claiming_free_reward(self):
        board = fixed_board()
        bench_elite = unit("sale", "真理", 2, elite=True)
        scene = state(phase="reward", funds=0, units=board + (bench_elite,), rewards=reward("哈洛德"))
        self.assertEqual((decide(scene).kind, decide(scene).unit_id), ("sell", bench_elite.id))
        self.assertEqual(decide(replace(scene, funds=1, units=board)).kind, "claim_reward")

    def test_tier_two_reward_prefers_other_roles_when_copy_counts_tie(self):
        scene = state(
            phase="reward",
            rewards=(Offer(0, "瑰盐", 2, 0, "医疗"), Offer(1, "泡泡", 2, 0, "重装"),
                     Offer(2, "Miss.Christine", 2, 0, "本源术师")),
        )
        self.assertEqual((decide(scene).kind, decide(scene).slot), ("claim_reward", 2))
        both_pairs = replace(scene, units=(unit("a", "泡泡", 2), unit("b", "Miss.Christine", 2)))
        self.assertEqual(decide(both_pairs).slot, 2)

    def test_reward_role_does_not_override_a_merge_or_pair(self):
        scene = state(
            phase="reward", units=(unit("a", "瑰盐", 2), unit("b", "泡泡", 2)),
            rewards=(Offer(0, "Miss.Christine", 2, 0, "本源术师"), Offer(1, "瑰盐", 2, 0, "医疗"),
                     Offer(2, "泡泡", 2, 0, "重装")),
        )
        self.assertEqual(decide(scene).slot, 1)
        tank_merge = replace(scene, units=scene.units + (unit("c", "泡泡", 2),))
        self.assertEqual(decide(tank_merge).slot, 2)

    def test_duqiao_is_the_last_free_reward_choice(self):
        bridge = Offer(0, "渡桥", 2, 0, "辅助")
        medic = Offer(1, "瑰盐", 2, 0, "医疗")
        scene = state(phase="reward", rewards=(bridge, medic),
                      units=(unit("first", "渡桥", 2), unit("second", "渡桥", 2)))
        self.assertEqual((decide(scene).kind, decide(scene).slot), ("claim_reward", medic.slot))
        only_choice = replace(scene, rewards=(bridge,))
        self.assertEqual((decide(only_choice).kind, decide(only_choice).slot), ("claim_reward", bridge.slot))

    def test_aosta_reward_precedes_an_ordinary_pair(self):
        scene = state(phase="reward", rewards=reward("真理", "渡桥", "奥斯塔"),
                      units=(unit("first", "真理", 2), unit("b1", "渡桥", 2), unit("b2", "渡桥", 2)))
        self.assertEqual((decide(scene).kind, decide(scene).slot), ("claim_reward", 2))

    def test_aosta_reward_breaks_a_merge_tie_but_yields_to_an_immediate_merge(self):
        truth_pair = (unit("t1", "真理", 2), unit("t2", "真理", 2))
        scene = state(phase="reward", rewards=reward("真理", "奥斯塔"), units=truth_pair)
        self.assertEqual((decide(scene).kind, decide(scene).slot), ("claim_reward", 0))
        aosta_pair = (unit("a1", "奥斯塔", 2), unit("a2", "奥斯塔", 2))
        both_can_merge = replace(scene, units=truth_pair + aosta_pair)
        self.assertEqual((decide(both_can_merge).kind, decide(both_can_merge).slot), ("claim_reward", 1))

    def test_chained_tier_two_rewards_before_last_round(self):
        scene = state(
            round_number=8, phase="reward", funds=0,
            units=(unit("r1", "哈洛德", 2), unit("r2", "哈洛德", 2), unit("r3", "真理", 2)),
            rewards=reward("真理", "哈洛德", "休露丝"),
        )
        self.assertEqual((decide(scene).kind, decide(scene).slot), ("claim_reward", 1))
        scene = replace(
            scene, units=(unit("elite", "哈洛德", 2, elite=True), unit("r3", "真理", 2)),
            rewards=reward("休露丝", "哈洛德", "真理"),
        )
        self.assertEqual((decide(scene).kind, decide(scene).unit_id), ("sell", "elite"))
        scene = replace(scene, units=scene.units[1:], funds=1)
        self.assertEqual((decide(scene).kind, decide(scene).slot), ("claim_reward", 2))

    def test_choose_reward_after_clearing_space(self):
        scene = state(
            phase="reward", bench_free=0, funds=0,
            units=fixed_board(elite=False) + (
                unit("a", "哈洛德", 2), unit("b", "哈洛德", 2),
                unit("sale", "真理", 2),
            ),
            rewards=reward("哈洛德", "真理"),
        )
        self.assertEqual((decide(scene).kind, decide(scene).unit_id), ("sell", "sale"))

    def test_freeze_a_completion_and_never_repeat(self):
        scene = state(
            funds=1, hp=11, units=(unit("a", "灰喉", tile="path0"), unit("b", "灰喉", tile="path1")),
            shop=shop("陨星", "灰喉"),
        )
        self.assertEqual(decide(scene).kind, "freeze")
        self.assertEqual(decide(replace(scene, frozen=True)).kind, "ready")
        self.assertEqual(decide(replace(scene, shop=shop("陨星"))).kind, "refresh")
        self.assertEqual(decide(replace(scene, funds=0, shop=shop("陨星"))).kind, "ready")

    def test_last_coin_freezes_pairs_before_or_after_refresh(self):
        scene = state(round_number=7, funds=1, hp=10, freeze_refresh_used=True,
                      units=(unit("own", "灰喉", tile="path0"),), shop=shop("灰喉"))
        for funds in (1, 0):
            with self.subTest(funds=funds):
                current = replace(scene, funds=funds)
                self.assertEqual(decide(current).kind, "freeze")
                self.assertEqual(decide(replace(current, shop=shop("角峰", "角峰"))).kind, "freeze")
                self.assertEqual(decide(replace(current, shop=shop("角峰"))).kind,
                                 "refresh" if funds else "ready")
        elite_only = replace(scene, units=(replace(scene.units[0], elite=True),))
        self.assertEqual(decide(elite_only).kind, "refresh")
        self.assertEqual(decide(replace(scene, round_number=8)).kind, "refresh")
        self.assertEqual(decide(replace(scene, round_number=9, shop=shop("角峰"))).kind, "exit")

    def test_buy_one_then_freeze_remaining_copy(self):
        scene = state(funds=2, hp=11, units=(unit("a", "灰喉", tile="path0"),), shop=shop("灰喉", "灰喉"))
        self.assertEqual(decide(scene).kind, "buy")
        scene = replace(
            scene, funds=0, units=scene.units + (unit("b", "灰喉", tile="path1"),),
            shop=scene.shop[1:],
        )
        self.assertEqual(decide(scene).kind, "freeze")

    def test_last_fund_buys_coin_as_part_of_freezing(self):
        coin = Offer(2, "盟约之币", 1, 1)
        other_gear = replace(coin, name="随身身份牌")
        scene = state(round_number=7, funds=1, hp=10, units=(unit("own", "灰喉", tile="path0"),),
                      shop=(coin,) + shop("灰喉"))
        action = decide(scene)
        self.assertEqual((action.kind, action.slot), ("freeze", coin.slot))
        for current in (replace(scene, coin_held=True), replace(scene, shop=(other_gear,) + shop("灰喉")),
                        replace(scene, funds=0, freeze_refresh_used=True)):
            action = decide(current)
            self.assertEqual((action.kind, action.slot), ("freeze", None))
        for current in (replace(scene, round_number=8), replace(scene, shop=(coin,))):
            action = decide(current)
            self.assertEqual((action.kind, action.slot), ("refresh", None))

    def test_keeps_frozen_completion_when_storage_stays_full(self):
        scene = state(
            funds=5, bench_free=0, frozen=True, blocked_tiles=SAFE_TILES + OTHER_TILES, units=fixed_board() + (
                unit("a", "灰喉"), unit("b", "灰喉"),
            ), shop=shop("灰喉"),
        )
        self.assertEqual(decide(scene).kind, "ready")
        self.assertEqual(decide(replace(scene, frozen=False)).kind, "freeze")

    def test_ninth_round_exits_before_purchases_or_refreshes(self):
        scene = state(
            round_number=9, funds=6, shop=shop("灰喉"),
            units=(unit("a", "灰喉", tile="path0"), unit("b", "灰喉", tile="path1")),
        )
        self.assertEqual(decide(scene).kind, "exit")
        self.assertEqual(decide(replace(scene, phase="reward", rewards=reward("哈洛德"))).kind, "exit")

    def test_older_round_nine_snapshot_can_still_be_loaded(self):
        scene = snapshot_from_dict({
            "round_number": 9, "funds": 6, "hp": 40, "bench_free": 4,
            "round9_promotions": 2, "round9_seconds": 30,
            "baseline_per_minute": 1, "boss_seconds": 120,
        })
        self.assertEqual(decide(scene).kind, "exit")

    def test_settlement_uses_confirmed_last_completed_round(self):
        per_round = {1: 1, 6: 2, 8: 1, 9: 2}
        self.assertEqual(settled_promotions(per_round, 8), 4)
        self.assertEqual(settled_promotions(per_round, 9), 6)


if __name__ == "__main__":
    unittest.main()
