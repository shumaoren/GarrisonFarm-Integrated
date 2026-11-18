"""One-action decisions for the level-one promotion route."""

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, replace
import argparse
import json
from pathlib import Path
import sys
import unicodedata


MODE = "极速模拟"
STRATEGY = "歌利亚"
SHOP_LEVEL = 1
EARLY_BUY_ROUNDS = 3
LATE_SHOP_ROUND = 6
LAST_ROUND = 9
FINAL_EXIT_REASON = "进入第9轮休整，退出并结算前8轮"
SAFE_TILES = ("safe0", "safe1")
OTHER_TILES = ("path0", "path1", "path2", "rest0", "rest1", "rest2")
FIELD_CAPACITY = len(SAFE_TILES) + len(OTHER_TILES)
DEPLOYMENT_BLACKLIST = frozenset({"渡桥"})
SALE_ROLE_ORDER = {"tank": 0, "medic": 1, "other": 2}
REFRESH_COST = 1
UNIT_COST = 2
FINAL_ROUND_START_PAIR_FUNDS = 3 * UNIT_COST + 3 * REFRESH_COST
COIN_NAME = "盟约之币"
COIN_COST = 1


def normalized_name(name):
    normalized = "".join(unicodedata.normalize("NFKC", name).split())
    return "折桠" if normalized == "折娅" else normalized


def operator_role(name, profession):
    name = normalized_name(name)
    profession = profession.strip().upper()
    if name == "折桠":
        return "other"
    if name in ("夏栎", "矩") or profession in ("医疗", "MEDIC"):
        return "medic"
    if profession in ("重装", "TANK"):
        return "tank"
    return "other"


@dataclass(frozen=True)
class Unit:
    id: str
    name: str
    tier: int
    profession: str
    elite: bool = False
    tile: str | None = None  # None means the unit is in the prep area.

    @property
    def key(self):
        return self.tier, normalized_name(self.name)

    @property
    def role(self):
        return operator_role(self.name, self.profession)


@dataclass(frozen=True)
class Offer:
    slot: int
    name: str
    tier: int
    cost: int
    profession: str = ""

    @property
    def key(self):
        return self.tier, normalized_name(self.name)

    @property
    def role(self):
        return operator_role(self.name, self.profession)


@dataclass(frozen=True)
class Snapshot:
    round_number: int
    funds: int
    hp: int
    bench_free: int
    phase: str = "prep"
    shop_level: int = SHOP_LEVEL
    units: tuple[Unit, ...] = ()
    shop: tuple[Offer, ...] = ()
    rewards: tuple[Offer, ...] = ()
    blocked_tiles: tuple[str, ...] = ()
    stalled_unit_ids: tuple[str, ...] = ()
    frozen: bool = False
    paid_refreshes: int = 0
    expected_future_shops: int | None = None
    freeze_refresh_used: bool = False
    coin_held: bool = False
    final_promotion_target: str | None = None
    final_promotion_done: bool = False

    @property
    def future_shops(self):
        estimate = self.expected_future_shops
        if estimate is None:
            estimate = max(0, (self.hp - 1) // 10)
        return max(0, min(estimate, LAST_ROUND - 1 - self.round_number))


@dataclass(frozen=True)
class Action:
    kind: str
    reason: str
    slot: int | None = None
    unit_id: str | None = None
    tile: str | None = None
    promotion_target: str | None = None


def _normal_counts(units):
    return Counter(unit.key for unit in units if not unit.elite)


def _group_offers(offers):
    groups = defaultdict(list)
    for offer in sorted(offers, key=lambda card: card.slot):
        groups[offer.key].append(offer)
    return groups


def _affordable(funds, cards):
    return sum(card.cost for card in cards) <= funds


def _deal_order(cards):
    return sum(card.cost for card in cards), cards[0].slot


def _completion_deals(groups, counts):
    deals = []
    for key, cards in groups.items():
        needed = 3 - counts[key]
        if 0 < needed <= len(cards):
            cheapest = sorted(cards, key=lambda card: (card.cost, card.slot))
            deals.append(tuple(cheapest[:needed]))
    return sorted(deals, key=_deal_order)


def _empty_tiles(state):
    placed = tuple(unit for unit in state.units if unit.tile is not None)
    occupied = set(state.blocked_tiles) | {unit.tile for unit in placed}
    if len(placed) >= FIELD_CAPACITY:
        return (), ()
    return (
        tuple(tile for tile in SAFE_TILES if tile not in occupied),
        tuple(tile for tile in OTHER_TILES if tile not in occupied),
    )


def _deployment(state, *, overflow=False):
    safe, other = _empty_tiles(state)
    bench = tuple(
        unit for unit in state.units
        if unit.tile is None and unit.id not in state.stalled_unit_ids
        and unit.key[1] not in DEPLOYMENT_BLACKLIST
    )
    medics = [unit for unit in bench if unit.role == "medic"]
    normal = [unit for unit in bench if unit.role == "other"]
    tanks = [unit for unit in bench if unit.role == "tank"]
    if safe and medics:
        return Action("deploy", "医疗使用木框位", unit_id=medics[0].id, tile=safe[0])
    open_tiles = other + safe
    if open_tiles and normal:
        return Action("deploy", "普通干员优先使用非木框位", unit_id=normal[0].id, tile=open_tiles[0])
    if overflow and other and medics:
        return Action("deploy", "腾整备格，允许医疗使用非木框位", unit_id=medics[0].id, tile=other[0])
    if overflow and tanks and (safe or other):
        tile = (safe + other)[0]
        return Action("deploy", "为接牌腾格，放宽重装限制", unit_id=tanks[0].id, tile=tile)
    return None


def _stall_action(state):
    for unit in state.units:
        if unit.id not in state.stalled_unit_ids or unit.tile is None or unit.tile in SAFE_TILES:
            continue
        if state.bench_free:
            return Action("withdraw", "上一轮该挡位拖到超时", unit_id=unit.id)
        safe, _ = _empty_tiles(state)
        if safe and unit.key[1] not in DEPLOYMENT_BLACKLIST:
            return Action("move", "将拖时挡位移至木框", unit_id=unit.id, tile=safe[0])
    return None


def _sale_rank(unit, counts, protected_key):
    role = unit.role
    if unit.elite:
        if role == "tank":
            return 0
        if role == "medic":
            return 1
        if unit.tier == 1:
            return 2
        return None
    if unit.key == protected_key or counts[unit.key] != 1:
        return None
    if unit.tier == 2:
        return 3
    if role == "tank":
        return 4
    if role == "medic":
        return 5
    return None


def _full_storage_sale_rank(unit, counts, protected_key):
    role = SALE_ROLE_ORDER[unit.role]
    if unit.elite:
        return 0, role, unit.tier if role == SALE_ROLE_ORDER["other"] else 0
    copies = counts[unit.key]
    if unit.key == protected_key or copies not in (1, 2) or unit.tier not in (1, 2):
        return None
    return copies, -unit.tier, role


def _sale_candidates(state, protected_key, rank_unit):
    counts = _normal_counts(state.units)
    candidates = []
    for order, unit in enumerate(state.units):
        rank = rank_unit(unit, counts, protected_key)
        if rank is not None:
            candidates.append((rank, unit.tile is not None, order, unit))
    return [unit for _, _, _, unit in sorted(candidates)]


def _sale(state, protected_key, *, full_field=False):
    rank_unit = _full_storage_sale_rank if full_field else _sale_rank
    for unit in _sale_candidates(state, protected_key, rank_unit):
        if unit.tile is not None:
            remaining = tuple(other for other in state.units if other.id != unit.id)
            if not _deployment(replace(state, units=remaining), overflow=True):
                continue
        return Action("sell", "整备满格，按售卖优先级腾位", unit_id=unit.id)
    return None


def _surplus_sale(state):
    if state.round_number == LAST_ROUND - 1:
        elite = min((unit for unit in state.units if unit.elite),
                    key=lambda unit: unit.tile is not None, default=None)
        if elite is not None:
            return Action("sell", f"第8轮出售精锐 {elite.name}", unit_id=elite.id)
        return None
    placed = tuple(unit for unit in state.units if unit.tile is not None)
    if len(placed) != FIELD_CAPACITY or any(unit.role == "tank" for unit in placed):
        return None
    for unit in state.units:
        if unit.tile is None and unit.elite:
            return Action("sell", f"8名非重装已上阵，出售整备区精锐 {unit.name}", unit_id=unit.id)
    return None


def _make_room(state, key):
    if state.bench_free:
        return _deployment(state) if state.round_number < LAST_ROUND - 1 else None
    return _reserve_bench_slot(state, key, allow_blocked_field_sale=True)


def _reserve_bench_slot(state, key, *, allow_blocked_field_sale=False):
    if state.bench_free:
        return None
    if sum(unit.tile is not None for unit in state.units) == FIELD_CAPACITY:
        return _sale(state, key, full_field=True)
    placement = _deployment(state, overflow=True)
    return placement or (_sale(state, key) if allow_blocked_field_sale else None)


def _inventory_action(state, key):
    room = _reserve_bench_slot(state, key)
    if state.round_number == LAST_ROUND - 1 and room and room.kind == "deploy":
        return _surplus_sale(state) or room
    return room or _surplus_sale(state)


def _can_buy_after_refresh(state):
    return bool(state.bench_free or _make_room(state, None))


def _buy(state, cards, reason, *, immediate_promotion=False):
    first = cards[0]
    if state.bench_free < len(cards):
        room = _make_room(state, first.key)
        if room:
            return room
        # A sale must wait until full. Allow the first buy only if there
        # is a way to free the next needed space, or we can freeze a partial deal.
        if not state.bench_free:
            return None
        if not (
            _make_room(replace(state, bench_free=0), first.key)
            or (immediate_promotion and state.future_shops)
        ):
            return None
    return Action("buy", reason, slot=first.slot)


def _final_promotion_sale(state, cards):
    shortage = sum(card.cost for card in cards) - state.funds
    if shortage <= 0:
        return None
    key = cards[0].key
    candidates = _sale_candidates(state, key, _full_storage_sale_rank)
    if len(candidates) <= shortage:
        return None
    unit = candidates[0]
    if state.bench_free < len(cards):
        unit = next((candidate for candidate in candidates if candidate.tile is None), unit)
    return Action("sell", f"第8轮为 {key[1]} 最后一次晋升筹资，差 {shortage} 资金，出售 {unit.name}",
                  unit_id=unit.id, promotion_target=key[1])


def _reward_action(state, counts):
    cards = sorted(
        (card for card in state.rewards if card.tier == 2 and card.cost == 0 and card.key[1]),
        key=lambda card: (
            card.key[1] in DEPLOYMENT_BLACKLIST,
            counts[card.key] < 2,
            card.key[1] != "奥斯塔",
            -counts[card.key],
            card.role != "other",
            card.slot,
        ),
    )
    if not cards:
        return Action("wait", "等待识别Ⅱ阶免费招募")
    choice = cards[0]
    inventory = _inventory_action(state, choice.key)
    if inventory:
        return inventory
    if not state.bench_free:
        room = _make_room(state, choice.key)
        return room or Action("wait", "免费Ⅱ阶招募缺少可腾出的整备格")
    return Action("claim_reward", "免费Ⅱ阶先晋升，再优先奥斯塔，其余先凑对，渡桥最后", slot=choice.slot)


def _next_purchase(state, groups, counts, completions, freeze_shop):
    for cards in completions:
        if _affordable(state.funds, cards):
            action = _buy(state, cards, "当前商店能完成晋升", immediate_promotion=True)
            if action:
                return action

    owned = Counter(count for (tier, _), count in counts.items() if tier == 1)
    pairs_owned = bool(owned[2])
    singles_owned = owned[1]
    last_shop = state.future_shops == 0
    opportunities = []
    for key, cards in groups.items():
        if counts[key] == 1:
            enough_to_continue = not pairs_owned and state.funds >= cards[0].cost + 3
            if not last_shop or enough_to_continue:
                opportunities.append(tuple(cards[:1]))
        elif counts[key] == 0 and len(cards) >= 2:
            enough_to_continue = not pairs_owned and state.funds >= sum(c.cost for c in cards[:2]) + 3
            worth_saving = not last_shop and (
                not pairs_owned or (state.round_number <= 5 and state.funds >= 8 and state.future_shops >= 2)
            )
            if enough_to_continue or worth_saving:
                opportunities.append(tuple(cards[:2]))
    for cards in sorted(opportunities, key=_deal_order):
        if _affordable(state.funds, cards):
            action = _buy(state, cards, "买成对子")
            if action:
                return action

    all_cards = sorted((card for cards in groups.values() for card in cards), key=lambda card: card.slot)
    if state.round_number <= EARLY_BUY_ROUNDS:
        for card in all_cards:
            if card.cost <= state.funds:
                action = _buy(state, (card,), f"第{state.round_number}轮优先收牌")
                if action:
                    return action

    if freeze_shop:
        return None

    if state.round_number < LAST_ROUND - 1 and state.funds == REFRESH_COST and not state.frozen:
        return Action("refresh", "剩1资金，当前店无同名牌，用掉最后1资金找可冻结的牌")

    if (
        state.round_number > 1 and state.funds == UNIT_COST
        and not state.freeze_refresh_used and state.future_shops and (pairs_owned or singles_owned)
    ):
        return Action("refresh", f"剩{state.funds}资金，最后刷新一次找可冻结的晋升")

    if state.round_number <= EARLY_BUY_ROUNDS:
        return None

    if (
        state.round_number == LAST_ROUND - 1 and not owned and state.bench_free
        and state.funds >= FINAL_ROUND_START_PAIR_FUNDS and all_cards and len(groups) == len(all_cards)
    ):
        return Action("buy", "第8轮没有Ⅰ阶普通牌，先买一张增加凑对机会", slot=all_cards[0].slot)

    can_start_name = (
        not pairs_owned and (
            (state.round_number < LATE_SHOP_ROUND and state.future_shops >= 2)
            or (state.round_number >= LATE_SHOP_ROUND and state.future_shops and singles_owned < 2)
        )
    )
    if can_start_name and state.bench_free >= 2:
        for card in all_cards:
            if counts[card.key] == 0 and card.cost <= state.funds:
                return Action("buy", "还有后续商店且无Ⅰ阶对子，建立新名字", slot=card.slot)

    copies_to_buy = 1
    if state.round_number >= LATE_SHOP_ROUND and not pairs_owned:
        if last_shop:
            copies_to_buy = 2 if singles_owned else 3
        elif not singles_owned:
            copies_to_buy = 2
    if state.funds >= REFRESH_COST + copies_to_buy * UNIT_COST and _can_buy_after_refresh(state):
        return Action("refresh", f"保留刷新后购买{copies_to_buy}张Ⅰ阶牌的资金")
    return None


def _finish(state, freeze_shop, groups, counts):
    if state.round_number < LAST_ROUND - 1:
        placement = _deployment(state)
        if placement:
            return placement
    if freeze_shop and not state.frozen:
        if state.round_number < LAST_ROUND - 1 and state.funds == COIN_COST and not state.coin_held:
            coin = next((card for card in state.shop if card.key == (1, COIN_NAME) and card.cost == COIN_COST), None)
            if coin is not None:
                return Action("freeze", "用最后1资金购买盟约之币，再冻结商店", slot=coin.slot)
        return Action("freeze", "把下轮能凑对或晋升的同名牌留在店里")
    if not state.frozen and state.funds >= REFRESH_COST:
        if state.round_number < LAST_ROUND - 1 and state.funds >= UNIT_COST and state.bench_free and groups:
            def purchase_order(cards):
                card = cards[0]
                copies = counts[card.key] + len(cards)
                return -copies, card.role == "tank", card.slot

            card = min(groups.values(), key=purchase_order)[0]
            return Action("buy", "开战前用余钱补Ⅰ阶干员", slot=card.slot)
        return Action("refresh", f"开战前剩{state.funds}资金，刷新找牌")
    return Action("ready", "完成该轮休整")


def decide(state: Snapshot) -> Action:
    """Recommend one action. Supply a fresh snapshot after executing it."""
    if state.phase == "squad_setup":
        return Action("confirm_squad", "按当前自选编队直接确认")
    if state.shop_level != SHOP_LEVEL:
        return Action("wait", "仅支持全程停Ⅰ级店")
    if not 1 <= state.round_number <= LAST_ROUND or not 0 <= state.bench_free <= 10:
        return Action("wait", "回合或整备区空位未确认")
    if state.round_number == LAST_ROUND and state.phase in ("prep", "reward"):
        return Action("exit", FINAL_EXIT_REASON)
    if state.funds < 0 or state.hp <= 0 or state.paid_refreshes < 0:
        return Action("wait", "资金、生命或刷新次数未确认")
    counts = _normal_counts(state.units)
    if state.phase == "reward":
        return _reward_action(state, counts)
    if state.phase != "prep":
        return Action("wait", "等待休整期")
    if state.round_number < LAST_ROUND - 1 and state.bench_free:
        action = _stall_action(state)
        if action:
            return action
    groups = _group_offers(
        card for card in state.shop if card.tier == 1 and card.cost == UNIT_COST and card.key[1]
    )
    completions = _completion_deals(groups, counts)
    final_round = state.round_number == LAST_ROUND - 1
    final_target = state.final_promotion_target if final_round else None
    protected_key = completions[0][0].key if completions else None
    if final_target is not None:
        protected_key = (SHOP_LEVEL, final_target)
        completions = [cards for cards in completions if cards[0].key == protected_key]
    action = _inventory_action(state, protected_key)
    if not action and not state.bench_free and state.round_number < LAST_ROUND - 1:
        action = _stall_action(state)
    if action:
        return action
    if final_round and state.final_promotion_done:
        return Action("ready", "已完成第8轮筹资晋升，完成该轮休整")
    if final_target is not None:
        if completions:
            cards = completions[0]
            if _affordable(state.funds, cards):
                action = _buy(state, cards, "买齐第8轮最后一次晋升", immediate_promotion=True)
            else:
                action = _final_promotion_sale(state, cards)
            if action:
                return action
        return Action("wait", f"无法继续第8轮筹资晋升 {final_target}，等待核对商店和整备区")
    last_coin = state.funds == REFRESH_COST or (state.funds == 0 and state.freeze_refresh_used)
    freeze_pair = state.round_number < LAST_ROUND - 1 and last_coin and any(
        counts[key] + len(cards) >= 2 for key, cards in groups.items()
    )
    freeze_shop = bool(state.future_shops and completions) or freeze_pair
    action = _next_purchase(state, groups, counts, completions, freeze_shop)
    if final_round and (action is None or action.kind == "refresh"):
        for cards in completions:
            sale = _final_promotion_sale(state, cards)
            if sale:
                return sale
    return action or _finish(state, freeze_shop, groups, counts)


def settled_promotions(round_counts, last_completed_round):
    """Sum observed merges through the round confirmed by settlement."""
    if not 0 <= last_completed_round <= LAST_ROUND:
        raise ValueError("结算回合超出极速模拟范围")
    if any(not 1 <= round_number <= LAST_ROUND or count < 0 for round_number, count in round_counts.items()):
        raise ValueError("晋升记录中的回合或次数超出范围")
    return sum(count for round_number, count in round_counts.items() if round_number <= last_completed_round)


def snapshot_from_dict(source):
    fields = dict(source)
    for obsolete in ("round9_promotions", "round9_seconds", "baseline_per_minute", "boss_seconds"):
        fields.pop(obsolete, None)
    if fields.get("phase") == "squad_setup":
        fields = {"round_number": 0, "funds": 0, "hp": 0, "bench_free": 0, **fields}
    for key, model in (("units", Unit), ("shop", Offer), ("rewards", Offer)):
        fields[key] = tuple(model(**item) for item in fields.get(key, ()))
    return Snapshot(**fields)


def main():
    parser = argparse.ArgumentParser(description="读取一级店局面，给出下一步决策")
    parser.add_argument("snapshot", help="UTF-8 JSON 文件，或 - 代表标准输入")
    args = parser.parse_args()
    try:
        if args.snapshot == "-":
            raw = sys.stdin.buffer.read().decode("utf-8-sig")
        else:
            raw = Path(args.snapshot).read_text(encoding="utf-8-sig")
        action = decide(snapshot_from_dict(json.loads(raw)))
    except (OSError, TypeError, ValueError) as error:
        parser.error(f"局面输入不可用：{error}")
    result = {key: value for key, value in asdict(action).items() if value is not None}
    sys.stdout.buffer.write((json.dumps(result, ensure_ascii=False) + "\n").encode("utf-8"))


if __name__ == "__main__":
    main()
